#!/usr/bin/env python3
"""
Database Connection Tester
ทดสอบการเชื่อมต่อ MySQL และตรวจสอบตารางต่างๆ
"""

import pymysql
import sys
from datetime import datetime

# ============================================================================
# Configuration
# ============================================================================

DB_CONFIG = {
    'host': 'localhost',
    'user': 'root',
    'password': 'Root1234!',  # ⚠️ เปลี่ยนเป็นรหัสผ่านจริง
    'database': 'your_db',
    'charset': 'utf8mb4',
    'cursorclass': pymysql.cursors.DictCursor
}

# ============================================================================
# Test Functions
# ============================================================================

def test_connection():
    """ทดสอบการเชื่อมต่อ database"""
    print("🔌 Testing database connection...")
    try:
        conn = pymysql.connect(**DB_CONFIG)
        print("✅ Connected successfully!")
        
        with conn.cursor() as cursor:
            cursor.execute("SELECT VERSION()")
            version = cursor.fetchone()
            print(f"   MySQL Version: {version['VERSION()']}")
            
            cursor.execute("SELECT DATABASE()")
            db = cursor.fetchone()
            print(f"   Database: {db['DATABASE()']}")
        
        conn.close()
        return True
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        return False

def test_tables():
    """ตรวจสอบว่ามีตารางครบหรือไม่"""
    print("\n📋 Checking tables...")
    required_tables = ['users', 'queue_entries', 'queue_history', 'scheduler_logs', 'system_stats']
    
    try:
        conn = pymysql.connect(**DB_CONFIG)
        with conn.cursor() as cursor:
            cursor.execute("SHOW TABLES")
            tables = [row[f'Tables_in_{DB_CONFIG["database"]}'] for row in cursor.fetchall()]
            
            print(f"   Found {len(tables)} tables:")
            for table in tables:
                status = "✅" if table in required_tables else "ℹ️"
                print(f"   {status} {table}")
            
            missing = set(required_tables) - set(tables)
            if missing:
                print(f"\n⚠️  Missing tables: {', '.join(missing)}")
                return False
            else:
                print("\n✅ All required tables exist!")
                return True
        
    except Exception as e:
        print(f"❌ Error checking tables: {e}")
        return False
    finally:
        conn.close()

def test_users():
    """ทดสอบตาราง users"""
    print("\n👥 Checking users table...")
    try:
        conn = pymysql.connect(**DB_CONFIG)
        with conn.cursor() as cursor:
            # นับจำนวน users
            cursor.execute("SELECT COUNT(*) as count FROM users")
            count = cursor.fetchone()['count']
            print(f"   Total users: {count}")
            
            # แสดง users ทั้งหมด
            cursor.execute("SELECT username, role, created_at FROM users")
            users = cursor.fetchall()
            
            if users:
                print("\n   Users:")
                for user in users:
                    print(f"   - {user['username']} ({user['role']}) - created: {user['created_at']}")
                return True
            else:
                print("   ⚠️  No users found!")
                return False
                
    except Exception as e:
        print(f"❌ Error checking users: {e}")
        return False
    finally:
        conn.close()

def test_insert_sample_queue():
    """ทดสอบการ insert ข้อมูลตัวอย่างใน queue"""
    print("\n🧪 Testing insert operation...")
    try:
        conn = pymysql.connect(**DB_CONFIG)
        with conn.cursor() as cursor:
            # Insert sample queue entry
            import uuid
            sql = """
            INSERT INTO queue_entries 
            (queue_id, name, phone, notes, priority, position, created_by, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """
            
            queue_id = str(uuid.uuid4())
            cursor.execute(sql, (
                queue_id,
                'ทดสอบ Test',
                '081-234-5678',
                'This is a test entry',
                0,
                1,
                'admin',
                'waiting'
            ))
            conn.commit()
            
            print(f"   ✅ Inserted test queue entry (ID: {queue_id})")
            
            # ดึงข้อมูลกลับมาเช็ค
            cursor.execute("SELECT * FROM queue_entries WHERE queue_id = %s", (queue_id,))
            entry = cursor.fetchone()
            
            if entry:
                print(f"   ✅ Verified: {entry['name']} - Position {entry['position']}")
                
                # ลบข้อมูลทดสอบ
                cursor.execute("DELETE FROM queue_entries WHERE queue_id = %s", (queue_id,))
                conn.commit()
                print(f"   🗑️  Cleaned up test data")
                return True
            else:
                print("   ❌ Could not verify inserted data")
                return False
                
    except Exception as e:
        print(f"❌ Insert test failed: {e}")
        return False
    finally:
        conn.close()

def test_all_queries():
    """ทดสอบ queries สำคัญๆ"""
    print("\n🔍 Testing important queries...")
    try:
        conn = pymysql.connect(**DB_CONFIG)
        with conn.cursor() as cursor:
            # Test 1: Current queue
            cursor.execute("""
                SELECT * FROM queue_entries 
                WHERE status = 'waiting' 
                ORDER BY position
            """)
            waiting = cursor.fetchall()
            print(f"   ✅ Current queue query OK (found {len(waiting)} entries)")
            
            # Test 2: Today's history
            cursor.execute("""
                SELECT * FROM queue_history 
                WHERE DATE(called_at) = CURDATE()
            """)
            today_history = cursor.fetchall()
            print(f"   ✅ Today's history query OK (found {len(today_history)} entries)")
            
            # Test 3: Statistics
            cursor.execute("""
                SELECT 
                    DATE(called_at) as date,
                    COUNT(*) as total,
                    AVG(wait_time_minutes) as avg_wait
                FROM queue_history
                GROUP BY DATE(called_at)
                ORDER BY date DESC
                LIMIT 7
            """)
            stats = cursor.fetchall()
            print(f"   ✅ Statistics query OK (found {len(stats)} days)")
            
            return True
            
    except Exception as e:
        print(f"❌ Query test failed: {e}")
        return False
    finally:
        conn.close()

def show_summary():
    """แสดงสรุปข้อมูลในฐานข้อมูล"""
    print("\n" + "="*60)
    print("📊 DATABASE SUMMARY")
    print("="*60)
    
    try:
        conn = pymysql.connect(**DB_CONFIG)
        with conn.cursor() as cursor:
            # Count tables
            cursor.execute("SHOW TABLES")
            tables_count = len(cursor.fetchall())
            
            # Count users
            cursor.execute("SELECT COUNT(*) as count FROM users")
            users_count = cursor.fetchone()['count']
            
            # Count current queue
            cursor.execute("SELECT COUNT(*) as count FROM queue_entries WHERE status = 'waiting'")
            queue_count = cursor.fetchone()['count']
            
            # Count history
            cursor.execute("SELECT COUNT(*) as count FROM queue_history")
            history_count = cursor.fetchone()['count']
            
            print(f"Tables:           {tables_count}")
            print(f"Users:            {users_count}")
            print(f"Current Queue:    {queue_count}")
            print(f"History Records:  {history_count}")
            
        conn.close()
        
    except Exception as e:
        print(f"Error: {e}")
    
    print("="*60)

# ============================================================================
# Main
# ============================================================================

def main():
    print("\n" + "="*60)
    print("🧪 DATABASE CONNECTION TESTER")
    print("="*60)
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Host: {DB_CONFIG['host']}")
    print(f"Database: {DB_CONFIG['database']}")
    print("="*60)
    
    results = []
    
    # Run all tests
    results.append(("Connection Test", test_connection()))
    
    if results[-1][1]:  # ถ้าเชื่อมต่อได้
        results.append(("Tables Test", test_tables()))
        results.append(("Users Test", test_users()))
        results.append(("Insert Test", test_insert_sample_queue()))
        results.append(("Queries Test", test_all_queries()))
        
        # Show summary
        show_summary()
    
    # Print results
    print("\n" + "="*60)
    print("📝 TEST RESULTS")
    print("="*60)
    
    for test_name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status}: {test_name}")
    
    all_passed = all(result[1] for result in results)
    
    print("="*60)
    if all_passed:
        print("🎉 ALL TESTS PASSED!")
        print("\n✅ Your database is ready to use!")
        print("\nNext steps:")
        print("1. Run your Flask app: python app_with_auth.py")
        print("2. Login with default credentials:")
        print("   - Admin: admin / admin123")
        print("   - User:  user / user123")
        return 0
    else:
        print("⚠️  SOME TESTS FAILED")
        print("\nPlease check:")
        print("1. MySQL is running")
        print("2. Database exists (run database_schema.sql)")
        print("3. Credentials are correct in DB_CONFIG")
        return 1

if __name__ == "__main__":
    try:
        exit_code = main()
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n\n⚠️  Test interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        sys.exit(1)
