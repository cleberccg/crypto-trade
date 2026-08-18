import pymysql

conn = pymysql.connect(host='127.0.0.1', user='root', password='', database='crypto_bot')
cursor = conn.cursor()

# Check if execution exists
cursor.execute("""
    SELECT execution_id, status, created_at, strategy, symbol 
    FROM optimization_runs 
    WHERE execution_id='3d217922-f817-4c0d-9a4e-78af98f2f6f7'
""")
result = cursor.fetchone()
if result:
    print(f"✅ Execution encontrada:")
    print(f"   ID: {result[0]}")
    print(f"   Status: {result[1]}")
    print(f"   Criada em: {result[2]}")
    print(f"   Strategy: {result[3]}")
    print(f"   Symbol: {result[4]}")
else:
    print("❌ Execution NÃO encontrada no banco")

# Check latest execution
cursor.execute("""
    SELECT execution_id, status, created_at, strategy
    FROM optimization_runs 
    ORDER BY created_at DESC 
    LIMIT 3
""")
results = cursor.fetchall()
print("\nÚltimas 3 execuções:")
for r in results:
    print(f"  {r[0][:8]}... | Status: {r[1]} | Criada: {r[2]} | Strategy: {r[3]}")

conn.close()
