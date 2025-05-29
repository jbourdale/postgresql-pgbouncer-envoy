import os
import time
import psycopg2
import random
import threading
import json
import signal
import sys
from flask import Flask, jsonify, request
from psycopg2 import pool
from prometheus_client import start_http_server, Counter, Gauge, Histogram
from contextlib import contextmanager

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

class PostgreSQLClient:
    def __init__(self):
        # Configuration with thread-safe access
        self._config_lock = threading.RLock()
        self._pool_lock = threading.RLock()
        
        # Database connection parameters
        self.db_host = os.environ.get('DB_HOST', 'localhost')
        self.db_port = os.environ.get('DB_PORT', 5432)
        self.db_user = os.environ.get('DB_USER', 'postgres')
        self.db_password = os.environ.get('DB_PASSWORD', 'postgres')
        self.db_name = os.environ.get('DB_NAME', 'postgres')
        
        # Configuration
        self._tps = int(os.environ.get('TPS', 10))
        self._pool_min_size = int(os.environ.get('POOL_MIN_SIZE', 40))
        self._pool_max_size = int(os.environ.get('POOL_MAX_SIZE', 40))
        
        # Connection pool
        self.connection_pool = None
        self._initialize_pool()
        
        # Control flags - use threading.Event for better shutdown coordination
        self.running = True
        self.shutdown_event = threading.Event()
        
    def _initialize_pool(self):
        """Initialize or recreate the connection pool"""
        with self._pool_lock:
            if self.connection_pool:
                self.connection_pool.closeall()
            
            self.connection_pool = pool.ThreadedConnectionPool(
                minconn=self._pool_min_size,
                maxconn=self._pool_max_size,
                host=self.db_host,
                port=self.db_port,
                user=self.db_user,
                password=self.db_password,
                dbname=self.db_name
            )
            
            # Update pool size metric
            POOL_SIZE.labels(client=client_name).set(self._pool_max_size)
    
    @contextmanager
    def get_connection(self):
        """Context manager for getting and returning connections"""
        conn = None
        try:
            with self._pool_lock:
                conn = self.connection_pool.getconn()
            yield conn
        finally:
            if conn:
                with self._pool_lock:
                    self.connection_pool.putconn(conn)
    
    @property
    def tps(self):
        with self._config_lock:
            return self._tps
    
    @tps.setter
    def tps(self, value):
        with self._config_lock:
            self._tps = max(1, int(value))  # Ensure TPS is at least 1
    
    @property
    def pool_min_size(self):
        with self._config_lock:
            return self._pool_min_size
    
    @property
    def pool_max_size(self):
        with self._config_lock:
            return self._pool_max_size
    
    def update_pool_size(self, min_size, max_size):
        """Update pool size and recreate the pool"""
        min_size = max(1, int(min_size))
        max_size = max(min_size, int(max_size))
        
        with self._config_lock:
            if min_size != self._pool_min_size or max_size != self._pool_max_size:
                print(f"[{client_name}] Updating pool size from {self._pool_min_size}-{self._pool_max_size} to {min_size}-{max_size}")
                self._pool_min_size = min_size
                self._pool_max_size = max_size
                self._initialize_pool()
                return True
        return False
    
    def get_config(self):
        """Get current configuration"""
        with self._config_lock:
            return {
                "tps": self._tps,
                "pool_min_size": self._pool_min_size,
                "pool_max_size": self._pool_max_size,
                "client_name": client_name
            }
    
    def get_pool_stats(self):
        """Get current pool statistics"""
        with self._pool_lock:
            if not self.connection_pool:
                return {"error": "Pool not initialized"}
            
            # Note: psycopg2 pool doesn't expose detailed stats directly
            # We'll approximate based on what we can measure
            return {
                "pool_size": self._pool_max_size,
                "pool_min_size": self._pool_min_size,
                "estimated_used": 0,  # This would need custom tracking for accuracy
                "estimated_available": self._pool_max_size
            }
    
    def run_query_loop(self):
        """Main query execution loop"""
        print(f"Starting {client_name} client with {self.tps} TPS")
        print(f"Connecting to {self.db_host}:{self.db_port}/{self.db_name} as {self.db_user}")
        print(f"Using connection pool with {self.pool_max_size} connections")
        
        while self.running and not self.shutdown_event.is_set():
            current_tps = self.tps
            sleep_time = 1.0 / current_tps
            
            try:
                # Update attempted queries counter
                QUERIES_TOTAL.labels(client=client_name).inc()
                QUERIES_RATE.labels(client=client_name).set(current_tps)
                
                # Execute query with connection from pool
                start_time = time.time()
                
                with self.get_connection() as conn:
                    # Update pool metrics (approximation)
                    POOL_USED.labels(client=client_name).set(1)  # Simplified tracking
                    POOL_AVAILABLE.labels(client=client_name).set(self.pool_max_size - 1)
                    
                    # Create a cursor and execute query
                    cur = conn.cursor()
                    cur.execute("SELECT 1")
                    result = cur.fetchone()
                    cur.close()
                
                # Record end time and calculate latency
                end_time = time.time()
                query_time = end_time - start_time
                
                # Update success counter and latency histogram
                QUERIES_SUCCESS.labels(client=client_name).inc()
                QUERY_LATENCY.labels(client=client_name).observe(query_time)
                
                print(f"[{client_name}] Query executed in {query_time:.6f} seconds, result: {result}, TPS: {current_tps}")
                
                # Use shutdown_event.wait() instead of time.sleep() for faster shutdown
                sleep_duration = max(0, sleep_time - query_time)
                if sleep_duration > 0:
                    self.shutdown_event.wait(sleep_duration)
                
            except Exception as e:
                # Record end time for failed queries too
                end_time = time.time()
                query_time = end_time - start_time if 'start_time' in locals() else 0
                
                # Update failed counter and latency histogram
                QUERIES_FAILED.labels(client=client_name).inc()
                QUERY_LATENCY.labels(client=client_name).observe(query_time)
                
                print(f"[{client_name}] Error executing query: {e}")
                
                # Use shutdown_event.wait() instead of time.sleep() for faster shutdown
                if not self.shutdown_event.is_set():
                    self.shutdown_event.wait(sleep_time)
        
        print(f"[{client_name}] Query loop stopped")
    
    def stop(self):
        """Stop the client"""
        print(f"[{client_name}] Stopping client...")
        self.running = False
        self.shutdown_event.set()
        
        # Close connection pool
        with self._pool_lock:
            if self.connection_pool:
                self.connection_pool.closeall()
                print(f"[{client_name}] Connection pool closed")

# Global client instance
pg_client = PostgreSQLClient()

# Flask app for API endpoints
app = Flask(__name__)

# Enhanced CORS middleware - Allow all origins for development
@app.after_request
def after_request(response):
    """Add CORS headers to all responses"""
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization,X-Requested-With')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    response.headers.add('Access-Control-Allow-Credentials', 'true')
    response.headers.add('Access-Control-Max-Age', '86400')  # Cache preflight for 24 hours
    return response

# Enhanced preflight OPTIONS handler
@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        response = app.response_class()
        response.headers.add('Access-Control-Allow-Headers', "Content-Type,Authorization,X-Requested-With")
        response.headers.add('Access-Control-Allow-Methods', "GET,PUT,POST,DELETE,OPTIONS")
        response.headers.add('Access-Control-Allow-Credentials', 'true')
        response.headers.add('Access-Control-Max-Age', '86400')
        response.status_code = 200
        return response

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({"status": "healthy", "client": client_name})

@app.route('/config', methods=['GET'])
def get_config():
    """Get current configuration"""
    return jsonify(pg_client.get_config())

@app.route('/config/tps', methods=['PUT'])
def update_tps():
    """Update TPS configuration"""
    try:
        data = request.get_json()
        if not data or 'tps' not in data:
            return jsonify({"error": "Missing 'tps' in request body"}), 400
        
        old_tps = pg_client.tps
        pg_client.tps = data['tps']
        
        return jsonify({
            "message": "TPS updated successfully",
            "old_tps": old_tps,
            "new_tps": pg_client.tps
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/config/pool', methods=['PUT'])
def update_pool():
    """Update pool size configuration"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Missing request body"}), 400
        
        min_size = data.get('min_size', pg_client.pool_min_size)
        max_size = data.get('max_size', pg_client.pool_max_size)
        
        old_config = {
            "min_size": pg_client.pool_min_size,
            "max_size": pg_client.pool_max_size
        }
        
        updated = pg_client.update_pool_size(min_size, max_size)
        
        if updated:
            return jsonify({
                "message": "Pool size updated successfully",
                "old_config": old_config,
                "new_config": {
                    "min_size": pg_client.pool_min_size,
                    "max_size": pg_client.pool_max_size
                }
            })
        else:
            return jsonify({
                "message": "No changes made to pool size",
                "current_config": {
                    "min_size": pg_client.pool_min_size,
                    "max_size": pg_client.pool_max_size
                }
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/stats', methods=['GET'])
def get_stats():
    """Get current statistics"""
    return jsonify({
        "config": pg_client.get_config(),
        "pool_stats": pg_client.get_pool_stats(),
        "client_name": client_name
    })

# Global shutdown event for coordinating shutdown across threads
shutdown_event = threading.Event()

def signal_handler(signum, frame):
    """Handle shutdown signals gracefully"""
    signal_name = 'SIGTERM' if signum == signal.SIGTERM else 'SIGINT'
    print(f"\n[{client_name}] Received {signal_name}, initiating graceful shutdown...")
    
    # Stop the PostgreSQL client
    pg_client.stop()
    
    # Set global shutdown event
    shutdown_event.set()
    
    # Exit the process
    sys.exit(0)

def main():
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    # Start Prometheus metrics server
    start_http_server(8000)
    
    # Start the query loop in a separate thread
    query_thread = threading.Thread(target=pg_client.run_query_loop, daemon=True)
    query_thread.start()
    
    # Start Flask API server
    api_port = int(os.environ.get('API_PORT', 8080))
    print(f"Starting API server on port {api_port}")
    print(f"Prometheus metrics available on port 8000")
    print(f"API endpoints:")
    print(f"  GET  /health - Health check")
    print(f"  GET  /config - Get current configuration")
    print(f"  PUT  /config/tps - Update TPS (JSON: {{\"tps\": number}})")
    print(f"  PUT  /config/pool - Update pool size (JSON: {{\"min_size\": number, \"max_size\": number}})")
    print(f"  GET  /stats - Get current statistics")
    
    try:
        # Run Flask with threaded=True for better signal handling
        app.run(host='0.0.0.0', port=api_port, debug=False, threaded=True)
    except (KeyboardInterrupt, SystemExit):
        print(f"[{client_name}] Shutting down...")
        pg_client.stop()
    except Exception as e:
        print(f"[{client_name}] Error in main: {e}")
        pg_client.stop()

if __name__ == "__main__":
    main()
