-- Create databases
CREATE DATABASE pooled_db;
CREATE DATABASE direct_db;

-- Create users with passwords
CREATE USER pooled_user WITH PASSWORD 'pooled_pass';
CREATE USER direct_user WITH PASSWORD 'direct_pass';

-- Grant privileges
GRANT ALL PRIVILEGES ON DATABASE pooled_db TO pooled_user;
GRANT ALL PRIVILEGES ON DATABASE direct_db TO direct_user;

-- Connect to each database and grant schema privileges
\c pooled_db;
GRANT ALL ON SCHEMA public TO pooled_user;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO pooled_user;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO pooled_user;

\c direct_db;
GRANT ALL ON SCHEMA public TO direct_user;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO direct_user;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO direct_user;

-- Create some test tables if needed
\c pooled_db;
CREATE TABLE IF NOT EXISTS test_table (
    id SERIAL PRIMARY KEY,
    data TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

\c direct_db;
CREATE TABLE IF NOT EXISTS test_table (
    id SERIAL PRIMARY KEY,
    data TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);