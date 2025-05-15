import os
import time
import psycopg2
import random
from prometheus_client import start_http_server, Counter, Gauge, Histogram

# Prometheus metrics
QUERIES_TOTAL = Counter('postgres_queries_total', 'Total number of PostgreSQL queries attempted')
QUERIES_SUCCESS = Counter('postgres_queries_success', 'Number of successful PostgreSQL queries')
QUERIES_FAILED = Counter('postgres_queries_failed', 'Number of failed PostgreSQL queries')
QUERIES_RATE = Gauge('postgres_queries_rate', 'Current rate of PostgreSQL queries per second')
QUERY_LATENCY = Histogram('postgres_query_latency_seconds', 'PostgreSQL query latency in seconds', 
                          buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10])

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
    
    # Calculate sleep time between queries
    sleep_time = 1.0 / tps
    
    print(f"Starting dummy client with {tps} TPS")
    print(f"Connecting to {db_host}:{db_port}/{db_name} as {db_user}")
    
    while True:
        try:
            # Update attempted queries counter
            QUERIES_TOTAL.inc()
            QUERIES_RATE.set(tps)
            
            # Connect to the database
            start_time = time.time()
            conn = psycopg2.connect(
                host=db_host,
                port=db_port,
                user=db_user,
                password=db_password,
                dbname=db_name
            )
            
            # Create a cursor
            cur = conn.cursor()
            
            # Execute a simple query
            cur.execute("SELECT 1")
            result = cur.fetchone()
            
            # Record end time and calculate latency
            end_time = time.time()
            query_time = end_time - start_time
            
            # Update success counter and latency histogram
            QUERIES_SUCCESS.inc()
            QUERY_LATENCY.observe(query_time)
            
            # Close cursor and connection
            cur.close()
            conn.close()
            
            print(f"Query executed in {query_time:.6f} seconds, result: {result}")
            
            # Sleep to maintain TPS
            time.sleep(max(0, sleep_time - query_time))
            
        except Exception as e:
            # Record end time for failed queries too
            end_time = time.time()
            query_time = end_time - start_time
            
            # Update failed counter and latency histogram
            QUERIES_FAILED.inc()
            QUERY_LATENCY.observe(query_time)
            
            print(f"Error executing query: {e}")
            time.sleep(sleep_time)

if __name__ == "__main__":
    main()
