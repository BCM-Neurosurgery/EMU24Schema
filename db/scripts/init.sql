CREATE DATABASE IF NOT EXISTS emu24_stitch_dev;
CREATE USER IF NOT EXISTS 'dj_user'@'%' IDENTIFIED BY 'dj_pass';

-- Grant access to both databases
GRANT ALL PRIVILEGES ON emu24_stitch_dev.* TO 'dj_user'@'%';
FLUSH PRIVILEGES;

-- Make sure all databases have appropriate tables - first stitch_inventory
USE emu24_stitch_dev;