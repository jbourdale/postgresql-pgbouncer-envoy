import os
import time
import psycopg2
import random
from psycopg2 import pool
from prometheus_client import start_http_server, Counter, Gauge, Histogram

# Get client name from environment (default to 'pooled' for the client going through envoy/pgbouncer)
client_name = os.environ.get('CLIENT_NAME', 'pooled')

# Prometheus metrics with client name label
QUERIES_TOTAL = Counter(f'postgres_queries_total', 'Total number of PostgreSQL queries attempted', 
                        ['client'])
QUERIES_SUCCESS = Counter(f'postgres_queries_success', 'Number of successful PostgreSQL queries', 
                          ['client'])
QUERIES_FAILED = Counter(f'postgres_queries_failed', 'Number of failed PostgreSQL queries', 
                         ['client'])
QUERIES_RATE = Gauge(f'postgres_queries_rate', 'Current rate of PostgreSQL queries per second', 
                     ['client'])
QUERY_LATENCY = Histogram(f'postgres_query_latency_seconds', 'PostgreSQL query latency in seconds', 
                          ['client'],
                          buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10])
POOL_SIZE = Gauge('postgres_connection_pool_size', 'Size of the PostgreSQL connection pool', 
                  ['client'])
POOL_AVAILABLE = Gauge('postgres_connection_pool_available', 'Available connections in the PostgreSQL pool', 
                       ['client'])
POOL_USED = Gauge('postgres_connection_pool_used', 'Used connections in the PostgreSQL pool', 
                  ['client'])

def main():
    # Start Prometheus metrics server
    start_http_server(8000)
    
    # Get environment variables
    tps = int(os.environ.get('TPS', 10))
    db_host = os.environ.get('DB_HOST', 'localhost')
    db_port = os.environ.get('DB_PORT', 5432)
    db_user = os.environ.get('DB_USER', 'postgres')
    db_password = os.environ.get('DB_PASSWORD', 'postgres')
    db_name = os.environ.get('DB_NAME', 'postgres')
    pool_min_size = int(os.environ.get('POOL_MIN_SIZE', 40))
    pool_max_size = int(os.environ.get('POOL_MAX_SIZE', 40))
    
    # Create a connection pool with 40 connections
    connection_pool = pool.ThreadedConnectionPool(
        minconn=pool_min_size,
        maxconn=pool_max_size,
        host=db_host,
        port=db_port,
        user=db_user,
        password=db_password,
        dbname=db_name
    )
    
    # Set initial pool metrics
    POOL_SIZE.labels(client=client_name).set(pool_max_size)
    
    # Calculate sleep time between queries
    sleep_time = 1.0 / tps
    
    print(f"Starting {client_name} client with {tps} TPS")
    print(f"Connecting to {db_host}:{db_port}/{db_name} as {db_user}")
    print(f"Using connection pool with {pool_max_size} connections")
    
    while True:
        try:
            # Update attempted queries counter
            QUERIES_TOTAL.labels(client=client_name).inc()
            QUERIES_RATE.labels(client=client_name).set(tps)
            
            # Get a connection from the pool
            start_time = time.time()
            conn = connection_pool.getconn()
            
            # Update pool metrics
            used_connections = pool_max_size - connection_pool.closed
            POOL_USED.labels(client=client_name).set(used_connections)
            POOL_AVAILABLE.labels(client=client_name).set(pool_max_size - used_connections)
            
            # Create a cursor
            cur = conn.cursor()
            
            # Execute a simple query
            cur.execute("SELECT 1")
            result = cur.fetchone()
            
            # Record end time and calculate latency
            end_time = time.time()
            query_time = end_time - start_time
            
            # Update success counter and latency histogram
            QUERIES_SUCCESS.labels(client=client_name).inc()
            QUERY_LATENCY.labels(client=client_name).observe(query_time)
            
            # Close cursor and return connection to pool
            cur.close()
            connection_pool.putconn(conn)
            
            print(f"[{client_name}] Query executed in {query_time:.6f} seconds, result: {result}")
            
            # Sleep to maintain TPS
            time.sleep(max(0, sleep_time - query_time))
            
        except Exception as e:
            # Record end time for failed queries too
            end_time = time.time()
            query_time = end_time - start_time
            
            # Update failed counter and latency histogram
            QUERIES_FAILED.labels(client=client_name).inc()
            QUERY_LATENCY.labels(client=client_name).observe(query_time)
            
            # If we got a connection, return it to the pool
            if 'conn' in locals() and conn is not None:
                connection_pool.putconn(conn)
            
            print(f"[{client_name}] Error executing query: {e}")
            time.sleep(sleep_time)

if __name__ == "__main__":
    main()
