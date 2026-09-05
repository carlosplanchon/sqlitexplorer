#!/usr/bin/env python3

from os.path import exists
import sqlite3
import argparse
import outfancy

def connect():
    """It connect with the database."""
    if exists(args.database_filename):
        try:
            global dbconnect
            global dbcursor
            dbconnect = sqlite3.connect(args.database_filename)
            dbcursor = dbconnect.cursor()
        except:
            raise NameError('--- THE CONNECTION WITH THE DATABASE CANNOT BE DONE ---')
    else:
        raise NameError('The database path you provide dont exists.')


def close_conection():
    """It closes the conection with the database."""
    dbconnect.commit()
    dbcursor.close()
    dbconnect.close()


# The rendering motor is created.
motor = outfancy.render.Table()
motor.set_show_errors(False)
motor.set_log_errors(False)
motor.set_check_data(False)
motor.set_check_table_size(False)

# The parser is created.
parser = argparse.ArgumentParser(description='A simple CLI sqlite3 explorer written in python3.')

# The arguments are added.
parser.add_argument('-db', dest='database_filename', type=str, help='the name of the database file.')
parser.add_argument('-table', dest='table', type=str, help='the name of the table to show.')
parser.add_argument('-query', dest='query', type=str, help='execute a query in the database.')

args = parser.parse_args()

if args.database_filename == None:
    print('You have to specify a database filename, use [db] filename')
else:
    # It try to do an SQLite query.
    if args.query != None:
        connect()
        dbcursor.execute(args.query)
        dataset = dbcursor.fetchall()
        print('--- QUERY EXECUTED ---')
        print(motor.render(dataset, label_list=False))
        close_conection()
    elif args.table != None:
        connect()
        # It tries to get the table information.
        try:
            dbcursor.execute('PRAGMA table_info(' + args.table + ')')
            table_info = dbcursor.fetchall()
        except:
            raise NameError('The specified table dont exists.')
        # It generates the label_list.
        label_list = []
        for x in range(len(table_info)):
            label_list.append(table_info[x][1])
        dbcursor.execute('SELECT * FROM ' + args.table)
        dataset = dbcursor.fetchall()
        print(motor.render(dataset, label_list=label_list))
        close_conection()
    else:
        connect()
        dbcursor.execute('SELECT * FROM sqlite_master')
        dataset = dbcursor.fetchall()
        print(motor.render(dataset, label_list=False))
        close_conection()
