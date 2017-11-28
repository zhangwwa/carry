import os
import pickle
import tempfile
from StringIO import StringIO

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

import config


class Source(object):
    def __init__(self):
        # type in ('csv', 'sql')
        self.type = None
        self.name = None

        # if type is sql
        self.engine = None
        self.use_view = None


source_dbs = {}

dest = create_engine(config.DATABASES['dest']['url'], echo=False)
dest_name = config.DATABASES['dest']['name']
for source_config in config.DATABASES['sources']:
    source = Source()
    if 'url' in source_config:
        source.type = 'sql'
        source.engine = create_engine(source_config['url'], echo=False)
        source.use_view = source_config.get('use_view', False)
    else:
        source.type = 'csv'
    source.name = source_config.get('name')
    source_dbs[source.name] = source

_path = os.path.join(tempfile.gettempdir(), 'ROMULAN_TOOLS_TEMP_FILE')


def get_df_from_source(name):
    sdb, sql = get_sdb(name), get_sdb_sql(name)
    if sdb.type == 'csv':
        # todo delete this
        header = sql.split('\n', 1)[0]
        if header.count('\t') > 0:
            df = pd.read_csv(StringIO(sql), sep='\t', dtype=object)
        else:
            df = pd.read_csv(StringIO(sql), dtype=object)

    else:
        if sdb.use_view:
            create_view(name, sdb, sql)
        df = pd.read_sql(sql, sdb.engine)
    print '[INSERT DATA]: {from_}.{name} -> {into}.{name}'.format(
        from_=sdb.name, into=dest_name, name=name)
    return df


def load_successful_tables():
    try:
        with open(_path) as fo:
            return pickle.load(fo)
    except IOError:
        return None


_successful_tables = []


def insert_successful_table(name):
    _successful_tables.append(freeze(name))


def dump_successful_tables():
    with open(_path, 'w') as fo:
        pickle.dump(_successful_tables, fo)


def truncate(names):
    if names:
        print '[TRUNCATE TABLE IN {}]: {}'.format(dest_name.upper(),
                                                  ', '.join(names))
        sql = """
        SET FOREIGN_KEY_CHECKS = 0;
        {}
        SET FOREIGN_KEY_CHECKS = 1;
        """.format('\n'.join('TRUNCATE {};'.format(name) for name in names))
        dest.execute(sql)


def create_view(name, sdb, sql):
    print '[CREATE VIEW]: {}.{}'.format(sdb.name.upper(), name)
    sql_ = """
    IF exists(SELECT *
        FROM sysobjects
            WHERE name = '{name}')
          BEGIN
            DROP VIEW {name}
          END
    """.format(name=name)
    sdb.engine.execute(text(sql_).execution_options(autocommit=True))
    sql_ = """
    CREATE VIEW {name} AS
    ({sql})
    """.format(name=name, sql=sql)
    sdb.engine.execute(sql_)


def freeze(d):
    if isinstance(d, dict):
        d = d.items()
        d.sort()
        return frozenset((key, freeze(value)) for key, value in d)
    elif isinstance(d, (list, tuple)):
        return tuple(freeze(value) for value in d)
    return d


class DFRowProxy(object):
    """
    row proxy for DataFrame
    """

    def __init__(self, idx, row, df):
        self.__dict__['_df'] = df
        self.__dict__['_idx'] = idx
        self.__dict__['_row'] = row

    def __setattr__(self, key, value):
        self._df.set_value(self._idx, key, value)

    def __getattr__(self, key):
        return self._row[key]


def main(refresh=True):
    # if refresh is True, insert all tables into destination database
    # if refresh is False, only insert table which the last migration was
    # failed or which is new in `ORDERS`
    if refresh:
        orders = config.ORDERS
        print 'FRESH\n'
    else:
        successful_orders = load_successful_tables()
        if successful_orders is None:
            orders = config.ORDERS
            print 'FRESH\n'
        else:
            successful_orders = set(successful_orders)

            config_orders = map(freeze, config.ORDERS)
            orders = list(name for name in config_orders if
                          name not in successful_orders)
            print 'NOT FRESH\n'

    # truncate tables in destination database
    if refresh:
        tables = getattr(config, 'TRUNCATES', list(orders))
    else:
        tables = orders

    tables = map(lambda x: x[0] if isinstance(x, (list, tuple)) else x, tables)
    truncate(tables)
    try:
        # insert
        for name in orders:
            if isinstance(name, (list, tuple)):
                table_name, option = name
            else:
                table_name = name
                option = None
            if option and 'before_script' in option:
                script_name = option['before_script']
                print '[EXECUTE SQL SCRIPT IN {}]: {}.sql'.format(
                    dest_name.upper(), script_name)
                sql = get_ddb_sql(script_name)
                dest.execute(sql)

            df = get_df_from_source(table_name)
            if option and 'before_insert' in option:
                callback = option['before_insert']
                for index, row in df.iterrows():
                    row = DFRowProxy(index, row, df)
                    callback(row)

            insert(df, table_name)

            if option and 'after_script' in option:
                script_name = option['after_script']
                print '[EXECUTE SQL SCRIPT IN {}]: {}.sql'.format(
                    dest_name.upper(), script_name)
                sql = get_ddb_sql(script_name)
                dest.execute(sql)

            insert_successful_table(name)
    except SQLAlchemyError:
        dump_successful_tables()
        raise

    # execute initial SQL in destination database
    for name in getattr(config, 'INITIALS', ()):
        print '[EXECUTE INITIAL SQL SCRIPT IN {}]: {}.sql'.format(
            dest_name.upper(), name)
        sql = get_ddb_sql(name)
        dest.execute(sql)


def get_sdb(name):
    dir_, _ = get_db_name_and_sql_path(name)
    return source_dbs[dir_]


def get_sdb_sql(name):
    _, sql_path = get_db_name_and_sql_path(name)
    with open(sql_path) as fo:
        return fo.read()


def get_ddb_sql(name):
    _, sql_path = get_db_name_and_sql_path(name, from_source=False)
    with open(sql_path) as fo:
        return fo.read()


_name_file_mapping = None


def get_db_name_and_sql_path(name, from_source=True):
    global _name_file_mapping
    if _name_file_mapping is None:
        _name_file_mapping = {
            'sources': {},
            'dest': {}
        }
        for basedir, dirs, filenames in os.walk(config.ROOT_DIR):
            for filename in filenames:
                root, ext = os.path.splitext(filename)
                if ext in ('.sql', '.csv'):
                    dir_ = os.path.basename(basedir)
                    if dir_ in source_dbs:
                        _name_file_mapping['sources'][root] = (
                            dir_,
                            os.path.join(basedir, filename))
                    elif dir_ == config.DATABASES['dest']['name']:
                        _name_file_mapping['dest'][root] = (
                            dir_,
                            os.path.join(basedir, filename))
                    else:
                        pass

    if from_source:
        return _name_file_mapping['sources'][name]
    else:
        return _name_file_mapping['dest'][name]


def insert(df, name):
    df.to_sql(name=name, con=dest, if_exists='append', index=False,
              chunksize=5000)


if __name__ == '__main__':
    main(refresh=True)
