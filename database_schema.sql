-- ============================================================================
-- Database Schema for Queue Management System with Authentication
-- ============================================================================

-- สร้าง Database (ถ้ายังไม่มี)
CREATE DATABASE IF NOT EXISTS techangel_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE techangel_db;

-- ============================================================================
-- Table: users
-- ============================================================================
-- เก็บข้อมูลผู้ใช้งาน (สำหรับ authentication)

CREATE TABLE IF NOT EXISTS users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(50) NOT NULL UNIQUE,
    password_hash VARCHAR(64) NOT NULL,
    role ENUM('user', 'admin') NOT NULL DEFAULT 'user',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    last_login DATETIME NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    
    INDEX idx_username (username),
    INDEX idx_role (role),
    INDEX idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================================================
-- Table: queue_entries
-- ============================================================================
-- เก็บข้อมูลคนในคิว (optional - ถ้าต้องการ persist ข้อมูลลง database)

CREATE TABLE IF NOT EXISTS queue_entries (
    id INT AUTO_INCREMENT PRIMARY KEY,
    queue_id VARCHAR(36) NOT NULL UNIQUE,
    name VARCHAR(100) NOT NULL,
    phone VARCHAR(20) NULL,
    notes TEXT NULL,
    priority INT NOT NULL DEFAULT 0,
    position INT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_by VARCHAR(50) NULL,
    expires_at DATETIME NULL,
    status ENUM('waiting', 'called', 'expired', 'cancelled') NOT NULL DEFAULT 'waiting',
    
    INDEX idx_queue_id (queue_id),
    INDEX idx_status (status),
    INDEX idx_created_at (created_at),
    INDEX idx_position (position),
    FOREIGN KEY (created_by) REFERENCES users(username) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================================================
-- Table: queue_history
-- ============================================================================
-- เก็บประวัติการเรียกคิว

CREATE TABLE IF NOT EXISTS queue_history (
    id INT AUTO_INCREMENT PRIMARY KEY,
    queue_id VARCHAR(36) NOT NULL,
    name VARCHAR(100) NOT NULL,
    phone VARCHAR(20) NULL,
    notes TEXT NULL,
    priority INT NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL,
    called_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    called_by VARCHAR(50) NULL,
    wait_time_minutes INT NULL,
    
    INDEX idx_queue_id (queue_id),
    INDEX idx_called_at (called_at),
    INDEX idx_called_by (called_by),
    FOREIGN KEY (called_by) REFERENCES users(username) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================================================
-- Table: scheduler_logs
-- ============================================================================
-- เก็บ log ของ scheduled tasks

CREATE TABLE IF NOT EXISTS scheduler_logs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    task_name VARCHAR(100) NOT NULL,
    status ENUM('success', 'error', 'running') NOT NULL,
    message TEXT NULL,
    details JSON NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    INDEX idx_task_name (task_name),
    INDEX idx_status (status),
    INDEX idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================================================
-- Table: system_stats
-- ============================================================================
-- เก็บสถิติของระบบ (optional)

CREATE TABLE IF NOT EXISTS system_stats (
    id INT AUTO_INCREMENT PRIMARY KEY,
    metric_name VARCHAR(100) NOT NULL,
    metric_value DECIMAL(15,2) NOT NULL,
    metric_unit VARCHAR(50) NULL,
    recorded_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    INDEX idx_metric_name (metric_name),
    INDEX idx_recorded_at (recorded_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================================================
-- Insert Default Admin User
-- ============================================================================
-- สร้าง admin account เริ่มต้น
-- password: admin123 (SHA256 hashed)

INSERT INTO users (username, password_hash, role, created_at) 
VALUES (
    'admin',
    '240be518fabd2724ddb6f04eeb1da5967448d7e831c08c8fa822809f74c720a9',
    'admin',
    NOW()
) ON DUPLICATE KEY UPDATE username=username;

-- สร้าง user account เริ่มต้น
-- password: user123 (SHA256 hashed)

INSERT INTO users (username, password_hash, role, created_at) 
VALUES (
    'user',
    '6ca13d52ca70c883e0f0bb101e425a89e8624de51db2d2392593af6a84118090',
    'user',
    NOW()
) ON DUPLICATE KEY UPDATE username=username;

-- ============================================================================
-- Sample Data (for testing)
-- ============================================================================

-- เพิ่ม sample queue entries (optional)
/*
INSERT INTO queue_entries (queue_id, name, phone, notes, priority, position, created_by) VALUES
    (UUID(), 'สมชาย ใจดี', '081-234-5678', 'ตรวจสุขภาพประจำปี', 0, 1, 'user'),
    (UUID(), 'สมหญิง รักงาม', '082-345-6789', 'ฉีดวัคซีน', 1, 2, 'user'),
    (UUID(), 'สมศักดิ์ มีชัย', '083-456-7890', NULL, 0, 3, 'admin');
*/

-- ============================================================================
-- Useful Queries
-- ============================================================================

-- ดูข้อมูลผู้ใช้ทั้งหมด
-- SELECT * FROM users;

-- ดูคิวปัจจุบัน
-- SELECT * FROM queue_entries WHERE status = 'waiting' ORDER BY position;

-- ดูประวัติคิววันนี้
-- SELECT * FROM queue_history WHERE DATE(called_at) = CURDATE();

-- ดูสถิติการใช้งาน
-- SELECT 
--     DATE(called_at) as date,
--     COUNT(*) as total_called,
--     AVG(wait_time_minutes) as avg_wait_time
-- FROM queue_history
-- GROUP BY DATE(called_at)
-- ORDER BY date DESC;

-- ============================================================================
-- Maintenance Queries
-- ============================================================================

-- ล้างข้อมูลเก่าที่หมดอายุ (เก็บแค่ 30 วัน)
-- DELETE FROM queue_history WHERE called_at < DATE_SUB(NOW(), INTERVAL 30 DAY);

-- ล้าง expired queue entries
-- DELETE FROM queue_entries WHERE status = 'expired' AND expires_at < DATE_SUB(NOW(), INTERVAL 7 DAY);

-- ล้าง scheduler logs เก่า
-- DELETE FROM scheduler_logs WHERE created_at < DATE_SUB(NOW(), INTERVAL 7 DAY);
