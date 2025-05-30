# PostgreSQL Query Quota System - Technical Specification

## Executive Summary

This document outlines the design and architecture for a real-time PostgreSQL query quota enforcement system. The system monitors database queries, calculates execution costs, and enforces per-user quotas to prevent resource abuse while maintaining zero impact on query performance.

## Problem Statement

### Business Requirements
- **Multi-tenant PostgreSQL environment** with hundreds of users sharing database resources
- **Resource abuse prevention** - prevent users from executing expensive queries that impact other users
- **Granular quota enforcement** - different limits for different user tiers (free, premium, enterprise)
- **Real-time monitoring** - track query costs and quota usage in near real-time
- **Zero performance impact** - quota system must not slow down legitimate database queries

### Technical Challenges
- **High traffic volume**: Production systems handle 50,000+ queries per second
- **Query cost estimation**: PostgreSQL costs vary dramatically based on data distribution and indexes
- **Normalization complexity**: Similar query patterns should share cost estimates despite different parameter values
- **Cache invalidation**: Query costs change as data grows and schema evolves
- **Enforcement timing**: Balance between real-time blocking and eventual consistency

## Architecture Overview

### Final Architecture Decision: Proxy-Based Traffic Duplication

After evaluating multiple approaches (eBPF observation, query interception, log parsing), the selected architecture uses **sidecar proxies** for traffic duplication with **asynchronous quota processing**.

```
Application → Sidecar Proxy → PgBouncer → PostgreSQL
                    ↓ (async)
              Quota Service → Redis Cache
                    ↓ (background)
              EXPLAIN Worker → Read Replica
                    ↓ (enforcement)
              PgBouncer Admin API
```

### Core Design Principles
1. **Zero Query Latency**: Original query path remains unmodified
2. **Asynchronous Processing**: All quota logic happens outside critical path
3. **Graceful Degradation**: System remains functional when quota service is unavailable
4. **Eventual Consistency**: Quota enforcement happens within seconds, not milliseconds
5. **Fail-Safe**: Unknown queries are allowed with conservative estimates

## Detailed Component Design

### 1. Sidecar Proxy

**Purpose**: Traffic duplication without performance impact

**Implementation Approach**:
- Deploy as sidecar container alongside application pods
- Applications connect to ```localhost:5432``` (proxy) instead of PgBouncer directly
- Proxy forwards all traffic to PgBouncer immediately
- Simultaneously duplicates TCP payload to quota service (async, non-blocking)

**Performance Characteristics**:
- **Latency Impact**: 0ms (async duplication)
- **Throughput**: 50,000+ concurrent connections per proxy instance
- **Memory Overhead**: ~4GB per proxy instance (connection state)
- **Failure Mode**: If proxy fails, database becomes unavailable (requires HA setup)

**Scaling Strategy**:
- Deploy as DaemonSet or sidecar (scales with application pods)
- Use Kubernetes Service for load balancing
- Anti-affinity rules to spread across nodes
- Health checks for automatic failover

### 2. Quota Service (High-Performance Design)

**Purpose**: Process query events and maintain quota state

**Architecture Pattern**: Worker pool with multi-level caching

**Performance Optimizations**:

#### Event Processing Pipeline
- **Batch Processing**: Collect 1,000 events before processing to amortize overhead
- **Worker Pool**: 16 parallel workers for CPU-intensive parsing
- **Zero-Copy Parsing**: Parse PostgreSQL wire protocol without string allocations
- **Lock-Free Operations**: Use atomic counters for quota updates

#### Caching Strategy
- **L1 Cache (Local Memory)**: 95% hit rate, <1μs lookup time
  - Query costs, user mappings, active quotas
  - LRU eviction, 10-minute TTL
  - Per-worker caches to avoid lock contention

- **L2 Cache (Redis)**: Persistent cache with batched operations
  - Pipeline 100 operations per Redis call
  - 24-hour TTL for query costs
  - Cluster setup for high availability

- **Background Refresh**: Proactively update hot data
  - Popular query patterns refreshed every hour
  - Heavy users' data kept in L1 cache permanently

#### Query Normalization
```sql
-- Original queries:
SELECT * FROM users WHERE id = 123 AND status = 'active'
SELECT * FROM users WHERE id = 456 AND status = 'pending'

-- Normalized cache key:
SELECT * FROM USERS WHERE ID = ? AND STATUS = ?

-- Representative query for EXPLAIN:
SELECT * FROM users WHERE id = 1 AND status = 'active'
```

**Performance Targets**:
- **Throughput**: 100,000 events/second per worker
- **Latency**: 100μs average processing time per event
- **Memory Usage**: 16GB (primarily for caches)
- **CPU Usage**: 24 cores (16 workers + overhead)

### 3. Cost Analysis System

**Purpose**: Determine execution cost for query patterns

**Challenge**: PostgreSQL EXPLAIN costs are highly sensitive to parameter values
```sql
-- Same pattern, different costs:
EXPLAIN SELECT * FROM users WHERE id = 999999;  -- Cost: 0.01 (non-existent)
EXPLAIN SELECT * FROM users WHERE id = 1;       -- Cost: 8.30 (normal lookup)
```

**Solution**: Generic Plan Analysis
- Convert queries to parameterized form (```$1```, ```$2```, etc.)
- Use PostgreSQL's ```PREPARE``` statements with ```GENERIC_PLAN``` option
- Get parameter-independent cost estimates
- Cache results by normalized query pattern

**Implementation Process**:
1. **Queue Unknown Queries**: Don't block processing for EXPLAIN operations
2. **Background Analysis**: Dedicated workers perform EXPLAIN on read replica
3. **Conservative Estimation**: Use high default costs for unknown patterns
4. **Cache Population**: Store results for future queries

**Fallback Strategy**:
- Use conservative cost estimates (1000 units) for unknown queries
- Process top 1000 query patterns first (80/20 rule)
- Sample-based analysis for long-tail queries

### 4. Quota Enforcement

**Philosophy**: Prevent sustained abuse, not individual expensive queries

**Enforcement Mechanism**: PgBouncer connection limits
- **Real-time quota tracking**: Update user quotas for every query
- **Batch enforcement**: Collect violating users every 10 seconds
- **Connection limiting**: Reduce max connections for violating users
- **Automatic recovery**: Remove limits when quotas reset

**Enforcement Actions**:
```
Quota Utilization: 0-80%    → No limits
Quota Utilization: 80-100%  → Warning logs
Quota Utilization: 100-150% → Max connections: 5
Quota Utilization: 150%+    → Max connections: 1
```

**Quota Reset Strategy**:
- **Time-based**: Reset quotas every hour/day
- **Rolling windows**: Track quota usage over sliding time windows
- **Burst allowance**: Allow temporary quota exceeding for bursty workloads

## Query Processing Flow

### Real-Time Path (Critical - Zero Latency)
1. Application sends SQL query to sidecar proxy
2. Proxy immediately forwards to PgBouncer
3. PgBouncer routes to PostgreSQL
4. Results flow back through proxy to application
5. **Total added latency: 0ms**

### Quota Processing Path (Async - Non-Critical)
1. Proxy duplicates TCP data to quota service (non-blocking)
2. Quota service batches events (1000 events or 10ms timeout)
3. Worker pool processes batch:
   - Parse PostgreSQL wire protocol
   - Normalize query (replace literals with ```?```)
   - Generate query hash for cache lookup
   - Resolve connection to user mapping
   - Check cost cache (L1 → L2 → background EXPLAIN)
   - Update user quota atomically
4. If quota exceeded, queue for enforcement
5. **Processing delay: 10-100ms**

### Background Processes
- **EXPLAIN Workers**: Analyze unknown query patterns
- **Cache Refresh**: Keep hot data current
- **Quota Enforcement**: Apply connection limits via PgBouncer API
- **Metrics Collection**: Export usage statistics

## Kubernetes Deployment

### Service Topology
```yaml
# Sidecar Proxy (DaemonSet)
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: postgres-proxy
spec:
  template:
    spec:
      containers:
      - name: proxy
        resources:
          requests: { cpu: 2000m, memory: 4Gi }
          limits: { cpu: 4000m, memory: 8Gi }

# Quota Service (Deployment)
apiVersion: apps/v1
kind: Deployment
metadata:
  name: quota-service
spec:
  replicas: 3
  template:
    spec:
      containers:
      - name: quota-service
        resources:
          requests: { cpu: 8000m, memory: 16Gi }
          limits: { cpu: 16000m, memory: 32Gi }

# Redis Cache (StatefulSet)
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: quota-cache
spec:
  replicas: 3
  template:
    spec:
      containers:
      - name: redis
        resources:
          requests: { cpu: 2000m, memory: 8Gi }
```

### Scaling Strategy
- **Horizontal Scaling**: Quota service scales based on event processing load
- **Vertical Scaling**: Increase cache sizes as user base grows
- **Auto-scaling**: HPA based on CPU usage and queue depth metrics

### High Availability
- **Multi-zone deployment**: Spread across availability zones
- **Graceful degradation**: Continue operations with reduced quota service capacity
- **Circuit breakers**: Disable quota processing if Redis is unavailable
- **Health checks**: Automatic pod replacement for failed instances

## Performance Characteristics

### Throughput Metrics
```
Query Processing: 50,000 QPS (per proxy instance)
Event Processing: 100,000 events/second (per quota service instance)
Cost Cache Lookups: 95% L1 hit rate, 5% L2 hit rate
Redis Operations: 5,000/second (batched operations)
EXPLAIN Queries: <100/second (only for cache misses)
```

### Latency Metrics
```
Query Latency Impact: +0ms (async processing)
Quota Processing: 100μs average per event
Cache Lookups: <1μs (L1), <10ms (L2 batched)
Quota Enforcement Delay: <10 seconds
Cost Analysis Delay: <60 seconds (background)
```

### Resource Requirements
```
Proxy Instances: 2 CPU, 4GB RAM (per 10k connections)
Quota Service: 16 CPU, 32GB RAM (per 100k QPS)
Redis Cluster: 6 CPU, 24GB RAM (for cost cache)
Total Overhead: ~15% additional infrastructure cost
```

## Operational Considerations

### Monitoring & Alerting
- **Query throughput**: Events processed per second
- **Cache hit rates**: L1 and L2 cache effectiveness
- **Quota violations**: Users exceeding limits
- **Processing delays**: Queue depth and processing latency
- **Error rates**: Failed parsing, Redis timeouts, PgBouncer errors

### Deployment Strategy
- **Blue-green deployment**: Zero-downtime updates
- **Feature flags**: Enable/disable quota enforcement per user group
- **Gradual rollout**: Start with monitoring only, gradually enable enforcement
- **Rollback plan**: Disable quota enforcement while maintaining monitoring

### Security Considerations
- **Network policies**: Restrict traffic between components
- **Authentication**: Secure communication with PgBouncer admin API
- **Data privacy**: Hash user identifiers, don't log query content
- **Access control**: Limit quota service permissions

### Disaster Recovery
- **Cache warming**: Rebuild cost cache from query logs if Redis fails
- **Graceful degradation**: Continue database operations without quota enforcement
- **Backup strategies**: Regular snapshots of quota configuration and user limits
- **Recovery procedures**: Step-by-step restoration of quota enforcement

## Implementation Phases

### Phase 1: Monitoring Only (4 weeks)
- Deploy proxy and quota service
- Implement query parsing and normalization
- Build cost analysis pipeline
- Create monitoring dashboards
- **Goal**: Understand query patterns without enforcement

### Phase 2: Background Enforcement (4 weeks)
- Implement quota tracking
- Add PgBouncer integration
- Create enforcement policies
- Add alerting for quota violations
- **Goal**: Enforce quotas for test users only

### Phase 3: Production Rollout (4 weeks)
- Gradual enablement across user tiers
- Performance optimization
- Operational runbooks
- Automated scaling configuration
- **Goal**: Full production deployment

### Phase 4: Advanced Features (8 weeks)
- Query recommendation engine
- Historical analytics
- Predictive quota adjustment
- Cost optimization suggestions
- **Goal**: Proactive resource management

## Risk Assessment & Mitigation

### High-Risk Scenarios
1. **Proxy Failure**: Applications can't reach database
   - **Mitigation**: Multi-instance deployment with health checks
   
2. **Quota Service Overload**: Events dropped, quotas not enforced
   - **Mitigation**: Auto-scaling, circuit breakers, degraded mode

3. **Cache Poisoning**: Incorrect costs cached permanently
   - **Mitigation**: TTL on all cache entries, manual cache invalidation

4. **False Positives**: Legitimate users blocked due to incorrect quotas
   - **Mitigation**: Conservative limits, manual override capabilities

### Medium-Risk Scenarios
1. **Cost Drift**: Query costs change as data grows
   - **Mitigation**: Periodic cost recalculation, trending analysis

2. **User Mapping Errors**: Queries attributed to wrong users
   - **Mitigation**: Connection tracking validation, audit logs

3. **Network Partitions**: Components can't communicate
   - **Mitigation**: Local caching, degraded operation modes

## Success Metrics

### Technical Metrics
- **Zero query latency impact**: <1ms added latency
- **High availability**: 99.9% uptime for quota system
- **Processing efficiency**: >95% cache hit rate
- **Scale handling**: Support 100k+ QPS

### Business Metrics
- **Resource abuse reduction**: 90% reduction in expensive query incidents
- **User satisfaction**: No complaints about legitimate queries being blocked
- **Operational efficiency**: 50% reduction in manual database intervention
- **Cost optimization**: 20% improvement in database resource utilization

## Key Design Decisions & Rationale

### Why Proxy-Based Instead of eBPF?
- **Operational Simplicity**: Standard Kubernetes deployment patterns
- **Development Speed**: Faster implementation and testing
- **Team Skills**: Doesn't require specialized eBPF knowledge
- **Debugging**: Easier to troubleshoot network-based systems
- **Trade-off**: Small performance overhead vs significantly reduced complexity

### Why Asynchronous Processing?
- **Zero Latency Impact**: Critical requirement for production systems
- **Scalability**: Can handle traffic spikes without blocking queries
- **Resilience**: System continues working if quota service has issues
- **Trade-off**: Eventual consistency vs real-time enforcement

### Why PgBouncer for Enforcement?
- **Efficiency**: Connection-level limits vs per-query decisions
- **Existing Infrastructure**: Leverage already deployed PgBouncer instances
- **Granular Control**: Fine-tuned connection and query time limits
- **Proven Technology**: Battle-tested in production environments

### Why Generic Plan Costs?
- **Cache Effectiveness**: Parameter-independent costs work well with caching
- **Consistency**: Same query pattern always gets same cost estimate
- **PostgreSQL Native**: Uses built-in generic planning mechanism
- **Trade-off**: Less precise than specific-parameter costs, but much more cacheable

This specification provides a comprehensive foundation for implementing a production-grade PostgreSQL quota system that balances performance, reliability, and operational simplicity.
