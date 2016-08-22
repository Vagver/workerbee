from __future__ import division

import logging
import os
import subprocess
import sys
import time

from distutils.version import StrictVersion
from postgres import Postgres
from psycopg2.extras import Json as postgres_jsonify

from .base import JobsExhaustedError, JobFailed, DEFAULT_LOGGER, timer, \
    exponential_decay

from string import ascii_letters, digits
if sys.version_info.major == 3:
    string_types = (str,)
else:
    string_types = (basestring,)
ALLOWED_CHARACTERS_IN_TABLE_NAME = set(ascii_letters.lower()) | set(digits) | set('_')
JSONB_POSTGRES_VER = StrictVersion('9.4')

# According to the postgres.py documentation, we should only have a single
# instantiation of the 'Postgres' class per database connection, per-process.
# So we need an ugly global to store the handles - which will be instantiated
# whenever the first db accessing method is called and is indexed by the
# connection info.
DB_HANDLES = {}


###############################################################################

TABLE_EXISTS_QUERY = r"""
SELECT EXISTS(
    SELECT *
    FROM information_schema.tables
    WHERE table_name = '{tbl_name}'
);
""".strip()

CREATE_TABLE_QUERY = r"""
CREATE TABLE {tbl_name}(
  id SERIAL PRIMARY KEY,
  input_data JSON{jsonb} NOT NULL,
  output_data JSON,
  n_claims INTEGER NOT NULL DEFAULT 0,
  time_last_completed TIMESTAMP WITH TIME ZONE,
  time_last_claimed TIMESTAMP WITH TIME ZONE,
  job_duration INTERVAL,
  n_failed_attempts INTEGER NOT NULL DEFAULT 0
)
""".strip()

INSERT_JOB_QUERY = r"""
INSERT INTO {tbl_name} (input_data) VALUES (%(input_data)s)
""".strip()

UNCOMPLETED_UNCLAIMED_ROW_QUERY = r"""
SELECT *
FROM {tbl_name} WHERE time_last_completed ISNULL AND time_last_claimed ISNULL
LIMIT 1;
""".strip()

OLDEST_UNCOMPLETED_ROW_QUERY = r"""
SELECT *
FROM {tbl_name}
WHERE time_last_completed ISNULL AND n_failed_attempts < %(max_n_retry_attempts)s
ORDER BY time_last_claimed
LIMIT 1;
""".strip()

SET_ROW_COMPLETED_BY_ID_QUERY = r"""
UPDATE {tbl_name}
SET time_last_completed=CURRENT_TIMESTAMP, output_data=%(output_data)s, job_duration=%(job_duration)s
WHERE id=%(id)s
""".strip()

SET_ROW_CLAIMED_BY_ID_QUERY = r"""
UPDATE {tbl_name} SET time_last_claimed=CURRENT_TIMESTAMP, n_claims = n_claims + 1
WHERE id=%(id)s
""".strip()

UPDATE_ROW_N_FAILED_ATTEMPTS_BY_ID_QUERY = r"""
UPDATE {tbl_name} SET n_failed_attempts = n_failed_attempts + 1
WHERE id=%(id)s
""".strip()

TOTAL_ROWS_QUERY = r"""
SELECT COUNT(*) FROM {tbl_name}
""".strip()

COMPLETED_ROWS_QUERY = r"""
SELECT COUNT(*) FROM {tbl_name} WHERE time_last_completed NOTNULL
""".strip()


###############################################################################


class DBConnectionInfo(object):

    def __init__(self, host=None, port=None, user=None, password=None,
                 database=None):
        self.host = host or os.environ.get('PGHOST', None)
        self.port = port or os.environ.get('PGPORT', None)
        self.user = user or os.environ.get('PGUSER', None)
        self.database = database or os.environ.get('PGDATABASE', None)
        self.password = password

    def missing_info(self):
        return None in {self.host, self.port, self.database, self.user}

    def postgres_connection_string(self):
        conn_str = 'host={host} port={port} user={user} dbname={db}'.format(
            db=self.database, user=self.user, host=self.host, port=self.port)
        if self.password:
            conn_str += ' password={}'.format(self.password)
        return conn_str

    def __eq__(self, other):
        return (isinstance(other, self.__class__) and
                self.__dict__ == other.__dict__)

    def __hash__(self):
        # Don't hash on the password.
        # Ensure that objects with the same connection info will hash the same
        return hash((self.host, self.port, self.user, self.database))

    def __str__(self):
        return '{db} on {user}{passw}@{host}:{port}'.format(
            db=self.database, user=self.user, host=self.host, port=self.port,
            passw=':{}'.format(self.password) if self.password else '')


def get_postgres_version():
    output = subprocess.check_output(['psql', '--version']).decode('utf-8')
    # Slice off version. Example expected output: psql (PostgreSQL) 9.3.14
    return StrictVersion(output.split(' ')[-1])


def get_db_handle(db_info=None, logger_name=DEFAULT_LOGGER):
    if db_info is None:
        db_info = DBConnectionInfo()

    if db_info in DB_HANDLES:
        handle = DB_HANDLES[db_info]
    else:
        if db_info.missing_info():
            raise ValueError('Unable to find the database configuration in the '
                             'local environment.')

        logger = logging.getLogger(logger_name)
        logger.info('Creating connection pool for {}'.format(db_info))
        if db_info.password is not None:
            logger.warn('Password is set via keyword argument. Note that this '
                        'is insecure and a ~/.pgpass file should be preferred.')
        else:
            logger.info('No password is set - the default behaviour of probing '
                        'the ~/.pgpass or PGPASSWORD environment variable '
                        'will be used.')

        DB_HANDLES[db_info] = Postgres(db_info.postgres_connection_string())
        handle = DB_HANDLES[db_info]
    return handle


def check_valid_table_name(table_name):
    if not isinstance(table_name, string_types):
        raise TypeError("Experiment ID '{}' is of type {}, not string".format(
            table_name, type(table_name)))
    invalid = set(table_name) - ALLOWED_CHARACTERS_IN_TABLE_NAME
    if len(invalid) > 0:
        invalid_c = ', '.join(["'{}'".format(l) for l in sorted(list(invalid))])
        raise ValueError("Invalid characters in experiment ID: {} "
                         "(allowed [a-z0-9_]+)".format(invalid_c))


def table_exists(db_handle, tbl_name):
    return db_handle.one(TABLE_EXISTS_QUERY.format(tbl_name=tbl_name))


def create_table(db_handle, tbl_name, logger_name=DEFAULT_LOGGER):
    logger = logging.getLogger(logger_name)
    pg_ver = get_postgres_version()
    # If Postgres >= 9.4 then create use jsonb as the 'input_data' data type
    if pg_ver>= JSONB_POSTGRES_VER:
        jsonb_input = 'b'
        logger.info('Found Postgresql version {} - Using JSONB as the data '
                    'type for the input_data field. UNIQUE constraints will '
                    'be enforced on input_data.'.format(pg_ver))
    else:
        jsonb_input = ''
        logger.warn('Found Postgresql version {} - Using JSON as the data '
                    'type for the input_data field. UNIQUE constraints will '
                    'NOT be enforced on input_data.'.format(pg_ver))
    db_handle.run(CREATE_TABLE_QUERY.format(tbl_name=tbl_name,
                                            jsonb=jsonb_input))


def get_uncompleted_unclaimed_job(db_handle, tbl_name):
    return db_handle.one(UNCOMPLETED_UNCLAIMED_ROW_QUERY.format(tbl_name=tbl_name))


def get_oldest_uncompleted_job(db_handle, tbl_name, max_n_retry_attempts):
    return db_handle.one(OLDEST_UNCOMPLETED_ROW_QUERY.format(tbl_name=tbl_name),
                         parameters={'max_n_retry_attempts': max_n_retry_attempts})


def set_job_as_complete(db_handle, tbl_name, job_id, duration,
                        output_data=None):
    if output_data is not None:
        output_data = postgres_jsonify(output_data)
    db_handle.run(SET_ROW_COMPLETED_BY_ID_QUERY.format(tbl_name=tbl_name),
                  parameters={'id': job_id, 'job_duration': duration,
                              'output_data': output_data})


def set_job_as_claimed(db_handle, tbl_name, job_id):
    db_handle.run(SET_ROW_CLAIMED_BY_ID_QUERY.format(tbl_name=tbl_name),
                  parameters={'id': job_id})


def update_job_n_failed_attempts(db_handle, tbl_name, job_id):
    db_handle.run(UPDATE_ROW_N_FAILED_ATTEMPTS_BY_ID_QUERY.format(tbl_name=tbl_name),
                  parameters={'id': job_id})


def get_total_job_count(db_handle, tbl_name):
    return db_handle.one(TOTAL_ROWS_QUERY.format(tbl_name=tbl_name))


def get_completed_job_count(db_handle, tbl_name):
    return db_handle.one(COMPLETED_ROWS_QUERY.format(tbl_name=tbl_name))

################################################################################


def add_job(experiment_id, input_data, db_connection_info=None,
            logger_name=DEFAULT_LOGGER, cursor=None):
    query_str = INSERT_JOB_QUERY.format(tbl_name=experiment_id)
    params = {'parameters': {'input_data': postgres_jsonify(input_data)}}
    if cursor is None:
        db_handle = get_db_handle(db_info=db_connection_info,
                                  logger_name=logger_name)

        if not table_exists(db_handle, experiment_id):
            raise ValueError("Table does not exist for experiment '{}'".format(
                experiment_id))

        db_handle.run(query_str, **params)
    else:
        cursor.run(query_str, **params)


def add_jobs(experiment_id, input_datas, db_connection_info=None,
             logger_name=DEFAULT_LOGGER, cursor=None):
    logger = logging.getLogger(logger_name)
    if cursor is None:
        db_handle = get_db_handle(db_info=db_connection_info,
                                  logger_name=logger_name)

        if not table_exists(db_handle, experiment_id):
            raise ValueError("Table does not exist for experiment '{}'".format(
                experiment_id))

        with db_handle.get_cursor() as cursor:
            for input_data in input_datas:
                add_job(experiment_id, input_data,
                        db_connection_info=db_connection_info,
                        logger_name=logger_name, cursor=cursor)
    else:
        for input_data in input_datas:
            add_job(experiment_id, input_data,
                    db_connection_info=db_connection_info,
                    logger_name=logger_name, cursor=cursor)
    logger.info('Submitted {} jobs'.format(len(input_datas)))


def setup_experiment(experiment_id, input_datas, db_connection_info=None,
                     logger_name=DEFAULT_LOGGER):
    db_handle = get_db_handle(db_info=db_connection_info,
                              logger_name=logger_name)
    logger = logging.getLogger(logger_name)

    check_valid_table_name(experiment_id)
    does_table_exist = table_exists(db_handle, experiment_id)

    if does_table_exist:
        logger.warn("Table already exists for experiment '{}'".format(experiment_id))
    else:
        logger.info("Creating table for experiment '{}'".format(experiment_id))
        with db_handle.get_cursor() as cursor:  # Single Transaction
            # Create table
            create_table(cursor, experiment_id)

            logger.info("Adding {} jobs to experiment '{}'".format(
                len(input_datas), experiment_id))

            # Fill in list of jobs
            add_jobs(experiment_id, input_datas,
                     db_connection_info=db_connection_info,
                     logger_name=logger_name, cursor=cursor)
        logger.info("Experiment '{}' set up with {} jobs.".format(
            experiment_id, len(input_datas)))


def postgres_worker(experiment_id, job_function, db_connection_info=None,
                    logger_name=DEFAULT_LOGGER, busywait=False,
                    max_busywait_sleep=None, max_failure_sleep=None,
                    max_n_retry_attempts=10):
    """

    Parameters
    ----------
    experiment_id : `str`
        A unique identifier for an experiment. Must be a valid Postgres Table
        name (no whitespace etc).
    job_function : `callable` taking (job_data : {})
        A callable that performs a unit of work in this experiment. The
        function will be provided with two arguments - the ``id`` for this
        job (a string) and the data payload for this job (a dictionary). This
        function then uses these inputs to perform the relevant work for the
        experiment. If the function completes without error, the job will be
        automatically marked complete in the database.
    """
    db_handle = get_db_handle(db_info=db_connection_info,
                              logger_name=logger_name)
    logger = logging.getLogger(logger_name)
    busywait_decay = exponential_decay(max_value=max_busywait_sleep)
    fail_decay = exponential_decay(max_value=max_failure_sleep)

    if not table_exists(db_handle, experiment_id):
        raise ValueError("Experiment '{}' does not exist - please run "
                         "setup_experiment first".format(experiment_id))

    try:
        while True:
            a_row = get_uncompleted_unclaimed_job(db_handle,
                                                  experiment_id)
            if a_row is None:
                # there is nothing left that is unclaimed, so we may as well
                # 'repeat' already claimed work - maybe we can beat another
                # worker to complete it.
                logger.info('No unclaimed work remains - re-claiming oldest job')
                a_row = get_oldest_uncompleted_job(db_handle, experiment_id,
                                                   max_n_retry_attempts)

            if a_row is None:
                if busywait:
                    d = next(busywait_decay)
                    logger.info('No uncompleted work - busywait with binary '
                                'exponential decay ({} seconds)'.format(d))
                    time.sleep(d)
                else:
                    raise JobsExhaustedError()
            else:
                # Reset the busywait
                busywait_decay = exponential_decay(max_value=max_busywait_sleep)

                # let's claim the job
                set_job_as_claimed(db_handle, experiment_id, a_row.id)

                logger.info('Claimed job (id: {})'.format(a_row.id))
                try:
                    with timer() as t:
                        output_data = job_function(a_row.input_data)
                except JobFailed as e:
                    d = next(fail_decay)
                    update_job_n_failed_attempts(db_handle, experiment_id, a_row.id)
                    logger.warn('Failed to complete job (id: {}) - sleeping '
                                'with binary exponential decay ({} seconds)'.format(
                                    a_row.id, d))
                    time.sleep(d)
                else:
                    # Reset the failure decay
                    fail_decay = exponential_decay(max_value=max_failure_sleep)

                    # Update the job information
                    set_job_as_complete(db_handle, experiment_id,
                                        a_row.id, t.interval,
                                        output_data=output_data)
                    logger.info('Completed job (id: {}) in {:.2f} seconds'.format(
                        a_row.id, t.interval.total_seconds()))
    except JobsExhaustedError:
        logger.info('All jobs are exhausted, terminating.')
