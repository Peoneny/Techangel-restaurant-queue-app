# คำแนะนำการตั้งค่า Database สำหรับ Queue Management System

## 📋 ขั้นตอนการติดตั้ง

### 1. ติดตั้ง MySQL (ถ้ายังไม่มี)

```bash
# สำหรับ Ubuntu/Debian
sudo apt update
sudo apt install mysql-server

# สำหรับ macOS (ใช้ Homebrew)
brew install mysql

# สำหรับ Windows
# ดาวน์โหลดจาก: https://dev.mysql.com/downloads/installer/
```

### 2. เข้าสู่ MySQL

```bash
mysql -u root -p
# ใส่รหัสผ่านที่ตั้งไว้ตอน install
```

### 3. สร้าง Database และ Import Schema

#### วิธีที่ 1: ใช้คำสั่ง MySQL
```bash
mysql -u root -p < database_schema.sql
```

#### วิธีที่ 2: ใน MySQL Shell
```sql
SOURCE /path/to/database_schema.sql;
```

#### วิธีที่ 3: Copy-Paste ใน MySQL Workbench
1. เปิดไฟล์ database_schema.sql
2. Copy ทั้งหมด
3. Paste ใน MySQL Workbench แล้วกด Execute

### 4. ตรวจสอบว่าสร้างสำเร็จ

```sql
USE your_db;
SHOW TABLES;
```

ควรเห็นตารางทั้งหมด:
- users
- queue_entries
- queue_history
- scheduler_logs
- system_stats

### 5. อัพเดทการตั้งค่าในไฟล์ Python

แก้ไขไฟล์ `app_with_auth.py` บรรทัด 38-45:

```python
def get_db():
    return pymysql.connect(
        host="localhost",        # เปลี่ยนเป็น IP ของ database ถ้าไม่ใช่เครื่องเดียวกัน
        user="root",             # เปลี่ยนเป็น username ของคุณ
        password="password",     # ⚠️ เปลี่ยนเป็นรหัสผ่านจริง
        database="your_db",      # ชื่อ database (ถ้าเปลี่ยนก็อัพเดทตรงนี้ด้วย)
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True
    )
```

## 🔐 บัญชีผู้ใช้เริ่มต้น

Schema มีการสร้างบัญชีเริ่มต้นให้แล้ว 2 บัญชี:

| Username | Password | Role  |
|----------|----------|-------|
| admin    | admin123 | admin |
| user     | user123  | user  |

⚠️ **แนะนำ:** เปลี่ยนรหัสผ่านทันทีหลังติดตั้ง!

### วิธีเปลี่ยนรหัสผ่าน admin

```python
import hashlib
new_password = "your_new_password"
hashed = hashlib.sha256(new_password.encode()).hexdigest()
print(hashed)  # Copy ค่านี้
```

แล้ว update ใน database:
```sql
UPDATE users 
SET password_hash = 'ค่าที่ได้จากด้านบน' 
WHERE username = 'admin';
```

## 🗂️ โครงสร้างตาราง

### 1. **users** - ข้อมูลผู้ใช้งาน
```sql
- id: รหัสอัตโนมัติ
- username: ชื่อผู้ใช้ (unique)
- password_hash: รหัสผ่านที่ hash แล้ว (SHA256)
- role: บทบาท (user หรือ admin)
- created_at: วันที่สร้างบัญชี
- last_login: ล็อกอินล่าสุดเมื่อไหร่
- is_active: เปิดใช้งานหรือไม่
```

### 2. **queue_entries** - คิวปัจจุบัน (optional)
```sql
- id: รหัสอัตโนมัติ
- queue_id: รหัสคิว (UUID)
- name: ชื่อผู้ใช้บริการ
- phone: เบอร์โทร
- notes: หมายเหตุ
- priority: ความสำคัญ (0 = ปกติ, 1+ = สำคัญ)
- position: ลำดับในคิว
- status: สถานะ (waiting/called/expired/cancelled)
- created_by: ใครเพิ่มเข้ามา
- expires_at: หมดอายุเมื่อไหร่
```

### 3. **queue_history** - ประวัติคิว
```sql
- id: รหัสอัตโนมัติ
- queue_id: รหัสคิวเดิม
- name: ชื่อผู้ใช้บริการ
- called_at: เวลาที่เรียก
- called_by: ใครเรียก (username)
- wait_time_minutes: รอนานเท่าไหร่
```

### 4. **scheduler_logs** - บันทึกการทำงานอัตโนมัติ
```sql
- task_name: ชื่อ task
- status: สถานะ (success/error/running)
- message: ข้อความ
- details: รายละเอียด (JSON)
```

### 5. **system_stats** - สถิติระบบ (optional)
```sql
- metric_name: ชื่อตัวชี้วัด
- metric_value: ค่า
- metric_unit: หน่วย
- recorded_at: เวลาที่บันทึก
```

## 📊 คำสั่ง SQL ที่มีประโยชน์

### ดูคิวปัจจุบัน
```sql
SELECT * FROM queue_entries 
WHERE status = 'waiting' 
ORDER BY position;
```

### ดูประวัติคิววันนี้
```sql
SELECT 
    name,
    called_at,
    called_by,
    wait_time_minutes
FROM queue_history 
WHERE DATE(called_at) = CURDATE()
ORDER BY called_at DESC;
```

### สถิติการใช้งานรายวัน
```sql
SELECT 
    DATE(called_at) as วัน,
    COUNT(*) as จำนวนคนที่เรียก,
    AVG(wait_time_minutes) as เวลารอเฉลี่ย_นาที,
    MIN(wait_time_minutes) as รอน้อยสุด,
    MAX(wait_time_minutes) as รอมากสุด
FROM queue_history
GROUP BY DATE(called_at)
ORDER BY วัน DESC
LIMIT 30;
```

### ล้างข้อมูลเก่า (เก็บแค่ 30 วัน)
```sql
DELETE FROM queue_history 
WHERE called_at < DATE_SUB(NOW(), INTERVAL 30 DAY);

DELETE FROM scheduler_logs 
WHERE created_at < DATE_SUB(NOW(), INTERVAL 7 DAY);
```

## 🔧 Troubleshooting

### ปัญหา: Connection refused
```bash
# ตรวจสอบว่า MySQL ทำงานหรือไม่
sudo systemctl status mysql

# Start MySQL
sudo systemctl start mysql
```

### ปัญหา: Access denied
```sql
-- สร้าง user ใหม่
CREATE USER 'your_user'@'localhost' IDENTIFIED BY 'your_password';
GRANT ALL PRIVILEGES ON your_db.* TO 'your_user'@'localhost';
FLUSH PRIVILEGES;
```

### ปัญหา: Can't connect from another machine
```sql
-- อนุญาตให้ connect จากเครื่องอื่น
CREATE USER 'your_user'@'%' IDENTIFIED BY 'your_password';
GRANT ALL PRIVILEGES ON your_db.* TO 'your_user'@'%';
FLUSH PRIVILEGES;
```

แล้วแก้ไขไฟล์ `/etc/mysql/mysql.conf.d/mysqld.cnf`:
```
# แก้จาก
bind-address = 127.0.0.1

# เป็น
bind-address = 0.0.0.0
```

แล้ว restart MySQL:
```bash
sudo systemctl restart mysql
```

## 🚀 ขั้นตอนการ Deploy บน Cloud

### สำหรับ Google Cloud SQL
```python
def get_db():
    return pymysql.connect(
        unix_socket=f'/cloudsql/{instance_connection_name}',
        user=os.environ.get('DB_USER'),
        password=os.environ.get('DB_PASS'),
        database=os.environ.get('DB_NAME'),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True
    )
```

### Environment Variables ที่ต้องตั้ง
```bash
DB_USER=your_db_user
DB_PASS=your_db_password
DB_NAME=your_db
INSTANCE_CONNECTION_NAME=project:region:instance
```

## 📝 หมายเหตุสำคัญ

1. **Security:** อย่าใช้รหัสผ่านเริ่มต้นในระบบจริง
2. **Backup:** ทำ backup database เป็นประจำ
   ```bash
   mysqldump -u root -p your_db > backup_$(date +%Y%m%d).sql
   ```
3. **Indexing:** ตารางมี index ครบแล้ว สำหรับ query ที่เร็ว
4. **Charset:** ใช้ utf8mb4 รองรับภาษาไทยและ emoji
5. **Connection Pooling:** ถ้าใช้งานหนัก ควรใช้ connection pool

## 🆘 ต้องการความช่วยเหลือ?

- ดู MySQL documentation: https://dev.mysql.com/doc/
- PyMySQL documentation: https://pymysql.readthedocs.io/
