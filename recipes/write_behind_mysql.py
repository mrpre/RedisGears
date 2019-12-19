import time

conn = None
sqlText = None
# addQuery = "insert into test_table values (%s, %s) ON DUPLICATE KEY UPDATE value=%s"
# deleteQuery = 'delete from test_table where test_table.key="%s"'

ADD_QUERY_KEY = '_add_query'
DEL_QUERY_KEY = '_delete_query'
TABLE_KEY = '_table'
KEY = '_key'

SLEEP_TIME=1

MYSQL_CONFIG = {
    ## see https://docs.sqlalchemy.org/en/13/core/engines.html for more info
    'ConnectionStr': '<db(mysql)>://<user>:<pass>@<addr>/<dbname>',
}

config = {
    'person2:id':{
        TABLE_KEY: 'person1',
        'first_name':'first',
        'last_name':'last',
        'age':'age',
    },
    'car:license':{
        'color':'color',
    },
}

def Log(msg, prefix='RedisGears - '):
    msg = prefix + msg
    try:
        execute('debug', 'log', msg)
    except Exception:
        print(msg)

def Connect():
    global conn
    global sqlText
    from sqlalchemy import create_engine
    from sqlalchemy.sql import text
    sqlText = text
    Log('connecting to database, ConnectionStr=%s' % (MYSQL_CONFIG['ConnectionStr']))
    conn = create_engine(MYSQL_CONFIG['ConnectionStr']).execution_options(autocommit=True)
    return conn

def PrepereQueries():
    for k,v in config.items():
        table, key = k.split(':')
        print(v)
        if TABLE_KEY not in v.keys():
            v[TABLE_KEY] = table
        v[KEY] = key
        if table is None or key is None:
            raise Exception('failed to create query for %s', str(k))

        # create insert query
        query = 'REPLACE INTO %s' % v[TABLE_KEY]
        values = [val for kk, val in v.items() if not kk.startswith('_')]
        values = [key] + values
        values.sort()
        query = '%s(%s) values(%s)' % (query, ','.join(values), ','.join([':%s' % a for a in values]))
        v[ADD_QUERY_KEY] = query

        # create delete query
        query = 'delete from %s where %s=:%s' % (v[TABLE_KEY], key, key)
        v[DEL_QUERY_KEY] = query

def PrintAllQueries():
    for v in config.values():
        Log('add_query="%s", del_query="%s"' % (v[ADD_QUERY_KEY], v[DEL_QUERY_KEY]))

def GetStreamName(config):
    return '_%s-stream-{%s}' % (config[TABLE_KEY], hashtag())

def CreateStreamInserter(config):
    def AddToStream(r):
        data = []
        data.append([config[KEY], r['key'].split(':')[1]])
        if 'value' in r.keys():
            keys = r['value'].keys()
            for kInHash, kInDB in config.items():
                if kInHash.startswith('_'):
                    continue
                if kInHash not in keys:
                    msg = 'Could not find %s in hash %s' % (kInHash, r['key'])
                    Log(msg)
                    raise Exception(msg)
                data.append([kInDB, r['value'][kInHash]])
        execute('xadd', GetStreamName(config), '*', *sum(data, []))
    return AddToStream

def CreateMySqlDataWriter(config):
    def WriteToMySql(r):
        global conn
        if(len(r) == 0):
            Log('Warning, got an empty batch')
            return
        for x in r:
            x.pop('streamId', None)## pop the stream id out of the record, we do not need it.
        while True:
            query = None
            errorOccured = False
            
            try:
                if not conn:
                    conn = Connect()
            except Exception as e:
                conn = None # next time we will reconnect to the database
                Log('Failed connecting to mysql database, will retry in %d second. error="%s"' % (SLEEP_TIME, str(e)))
                time.sleep(SLEEP_TIME)
                continue # lets retry

            try:
                batch = []
                isAddBatch = True if len(r[0].keys()) > 1 else False # we have only key name, it means that the key was deleted
                query = config[ADD_QUERY_KEY] if isAddBatch else config[DEL_QUERY_KEY]
                for x in r:
                    if len(x.keys()) == 1: # we have only key name, it means that the key was deleted
                        if isAddBatch:
                            conn.execute(sqlText(query), batch)
                            batch = []
                            isAddBatch = False
                            query = config[DEL_QUERY_KEY]
                        batch.append(x)
                    else:
                        if not isAddBatch:
                            conn.execute(sqlText(query), batch)
                            batch = []
                            isAddBatch = True
                            query = config[ADD_QUERY_KEY]
                        batch.append(x)
                if len(batch) > 0:
                    conn.execute(sqlText(query), batch)
            except Exception as e:
                Log('Got exception when writing to mysql, query="%s", error="%s".' % ((query if query else 'None'), str(e)))
                errorOccured = True

            if errorOccured:
                conn = None # next time we will reconnect to the database
                Log('Error occured while running the sql transaction, will retry in %d second.' % SLEEP_TIME)
                time.sleep(SLEEP_TIME)
                continue # lets retry
            return # we finished successfully, lets break the retry loop
    return WriteToMySql

def RegisterExecutions():
    for v in config.values():

        ## create the execution to write each changed key to stream
        GB('KeysReader', desc='add each changed key with prefix %s* to Stream' % v[TABLE_KEY]).\
        filter(lambda x: x['key'] != GetStreamName(v)).\
        foreach(CreateStreamInserter(v)).\
        register(mode='sync', regex='%s:*' % v[TABLE_KEY])

        ## create the execution to write each key from stream to mysql
        GB('StreamReader', desc='read from stream and write to mysql table %s' % v[TABLE_KEY]).\
        aggregate([], lambda a, r: a + [r], lambda a, r: a + r).\
        foreach(CreateMySqlDataWriter(v)).\
        count().\
        register(regex='_%s-stream-*' % v[TABLE_KEY], mode="async_local", batch=100, duration=4000)


PrepereQueries()

PrintAllQueries()

RegisterExecutions()