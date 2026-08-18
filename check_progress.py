import pymysql

EXECUTION_ID = '3d217922-f817-4c0d-9a4e-78af98f2f6f7'

conn = pymysql.connect(host='127.0.0.1', user='root', password='', database='crypto_bot')
cursor = conn.cursor()

cursor.execute(
    "SELECT status, started_at, finished_at, total_combinations, workers FROM optimization_runs WHERE execution_id=%s",
    (EXECUTION_ID,),
)
run = cursor.fetchone()
if run:
    print(f'Status: {run[0]}')
    print(f'Started: {run[1]}')
    print(f'Finished: {run[2]}')
    print(f'Total combinations: {run[3]}')
    print(f'Workers: {run[4]}')

cursor.execute(
    "SELECT COUNT(*), SUM(CASE WHEN profit_factor IS NOT NULL THEN 1 ELSE 0 END) FROM optimization_results_history WHERE execution_id=%s",
    (EXECUTION_ID,),
)
count, complete = cursor.fetchone()
complete = complete or 0
print(f'Processed rows: {count}/500')
print(f'Rows with metrics: {complete}')

cursor.execute("SHOW COLUMNS FROM optimization_results_history")
columns = [row[0] for row in cursor.fetchall()]
metric_columns = [
    col for col in columns
    if col not in {'id', 'execution_id', 'strategy', 'symbol', 'timeframe', 'parameters', 'created_at', 'updated_at'}
]
print('Metric columns: ' + ', '.join(metric_columns))

if count:
    metric_select = []
    for column in ['profit_factor', 'expectancy', 'win_rate', 'drawdown', 'net_profit', 'trades', 'sharpe']:
        if column in columns:
            metric_select.append(f'MAX({column}) AS {column}')
    if metric_select:
        cursor.execute(
            f"SELECT {', '.join(metric_select)} FROM optimization_results_history WHERE execution_id=%s",
            (EXECUTION_ID,),
        )
        metrics = cursor.fetchone()
        print('Best metrics:')
        for column, value in zip([part.split(' AS ')[-1] for part in metric_select], metrics):
            print(f'  {column}: {value}')

conn.close()
