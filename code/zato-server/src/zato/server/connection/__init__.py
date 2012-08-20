# -*- coding: utf-8 -*-

"""
Copyright (C) 2011 Dariusz Suchojad <dsuch at gefira.pl>

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

from __future__ import absolute_import, division, print_function, unicode_literals

# Setting the custom logger must come first
import logging
from zato.server.log import ZatoLogger
logging.setLoggerClass(ZatoLogger)

# stdlib
import errno, os, sys, time
from datetime import datetime
from subprocess import Popen
from traceback import format_exc

# psutil
import psutil

# ZeroMQ
import zmq

# Bunch
from bunch import Bunch

# Zato
from zato.common import ZATO_ODB_POOL_NAME
from zato.common.util import get_app_context, get_config, get_crypto_manager, TRACE1
from zato.server.base import BaseWorker
from zato.server.kvdb import KVDB

class BaseConnection(object):
    """ A base class for connections to any external resourced accessed through
    connectors. Implements the (re-)connection logic and leaves all the particular
    details related to messaging to subclasses.
    """
    def __init__(self):
        self.reconnect_error_numbers = (errno.ENETUNREACH, errno.ENETRESET, errno.ECONNABORTED, 
            errno.ECONNRESET, errno.ETIMEDOUT, errno.ECONNREFUSED, errno.EHOSTUNREACH)
        self.reconnect_exceptions = ()
        self.connection_attempts = 1
        self.first_connection_attempt_time = None
        self.keep_connecting = True
        self.reconnect_sleep_time = 5 # Seconds
        self.has_valid_connection = False

    def _start(self):
        """ Actually start a specific resource.
        """ 
        raise NotImplementedError('Must be implemented by a subclass')
    
    def _close(self):
        """ Perform a resource-specific close operation.
        """
        raise NotImplementedError('Must be implemented by a subclass')

    def _conn_info(self):
        """ A textual information regarding the connection for logging purposes.
        """
        raise NotImplementedError('Must be implemented by a subclass')
    
    def _keep_connecting(self, e):
        """ Invoked on an exception being caught during establishing a connection.
        Receives the exception object and has to answer whether to keep on (re-)connecting.
        """
        raise NotImplementedError('Must be implemented by a subclass')
    
    def _run(self):
        """ Run the main (re-)connecting loop, close on Ctrl-C.
        """ 
        try:
            self.start()
        except KeyboardInterrupt:
            self.close()
    
    def close(self):
        """ Attempt to close the connection to an external resource.
        """
        if(self.logger.isEnabledFor(TRACE1)):
            msg = 'About to close the connection for {0}'.format(self._conn_info())
            self.logger.log(TRACE1, msg)
            
        self.keep_connecting = False
        self._close()
            
        msg = 'Closed the connection for {0}'.format(self._conn_info())
        self.logger.debug(msg)
    
    def _on_connected(self, *ignored_args, **ignored_kwargs):
        """ Invoked after establishing a successful connection to the resource.
        Will report a diagnostic message regarding how many attempts there were
        and how long it took if the connection hadn't been established straightaway.
        """
        if self.connection_attempts > 1:
            delta = datetime.utcnow() - self.first_connection_attempt_time
            msg = '(Re-)connected to {0} after {1} attempt(s), time spent {2}'.format(
                self._conn_info(), self.connection_attempts, delta)
            self.logger.warn(msg)
            
        if self.has_valid_connection:
            self.connection_attempts = 1
    
    def start(self):
        """ Start the connection, reconnect on any recoverable errors.
        """ 
        self.first_connection_attempt_time = datetime.utcnow() 
        
        def _no_valid_connection(e=None):
            if e:
                if isinstance(e, EnvironmentError):
                    err_info = '{0} {1}'.format(e.errno, e.strerror)
                else:
                    err_info = format_exc(e)
                prefix = 'Caught [{}] error'.format(err_info)
            else:
                prefix = 'Could not establish the connection (Invalid credentials? Is the connection being shut down?)'
                
            msg = prefix + ', will try to (re-)connect to {} in {} seconds, {} attempt(s) so far, time spent {}'
            delta = datetime.utcnow() - self.first_connection_attempt_time
            self.logger.warn(msg.format(self._conn_info(), self.reconnect_sleep_time, self.connection_attempts, delta))
            self.connection_attempts += 1
            time.sleep(self.reconnect_sleep_time)

        while self.keep_connecting:
            try:
                
                # Actually try establishing the connection
                self._start()
                
                # Set only if there was an already established connection 
                # and we're now trying to reconnect to the resource.
                if self.has_valid_connection:
                    self.first_connection_attempt_time = datetime.utcnow()
            except self.reconnect_exceptions, e:
                if self._keep_connecting(e):
                    _no_valid_connection(e)
                else:
                    msg = 'No connection for {0}, e:[{1}]'.format(self._conn_info(), format_exc(e))
                    self.logger.error(msg)
                    raise
            else:
                if not self.has_valid_connection:
                    _no_valid_connection()

class BaseConnector(BaseWorker):
    """ A base class for both channels and outgoing connectors.
    """
    def __init__(self, repo_location, def_id):
        self.repo_location = repo_location
        self.def_id = def_id
        self.odb = None
        self.sql_pool_store = None
        
    def _close(self):
        """ Close the process, don't forget about the ODB connection if it exists.
        """
        if self.odb:
            self.odb.close()
        p = psutil.Process(os.getpid())
        p.terminate()
    
    def _setup_odb(self):
        # First let's see if the server we're running on top of exists in the ODB.
        self.server = self.odb.fetch_server()
        if not self.server:
            raise Exception('Server does not exist in the ODB')
        
    def _init(self):
        """ Initializes all the basic run-time data structures and connects
        to the Zato broker.
        """
        fs_server_config = get_config(self.repo_location, 'server.conf')
        app_context = get_app_context(fs_server_config)
        crypto_manager = get_crypto_manager(self.repo_location, app_context, fs_server_config)
        
        config_odb = fs_server_config.odb
        self.odb = app_context.get_object('odb_manager')
        self.odb.crypto_manager = crypto_manager
        self.odb.odb_token = config_odb.token
        
        # Key-value DB
        self.kvdb = KVDB()
        self.kvdb.config = fs_server_config.kvdb
        self.kvdb.decrypt_func = self.odb.crypto_manager.decrypt
        self.kvdb.init()
        
        odb_data = Bunch()
        odb_data.db_name = config_odb.db_name
        odb_data.engine = config_odb.engine
        odb_data.extra = config_odb.extra
        odb_data.host = config_odb.host
        odb_data.password = self.odb.crypto_manager.decrypt(config_odb.password)
        odb_data.pool_size = config_odb.pool_size
        odb_data.username = config_odb.username
        odb_data.is_odb = True
        
        self.sql_pool_store = app_context.get_object('sql_pool_store')
        self.sql_pool_store[ZATO_ODB_POOL_NAME] = odb_data
        self.odb.pool = self.sql_pool_store[ZATO_ODB_POOL_NAME]
        
        self._setup_odb()

        # Connects to the broker
        super(BaseConnector, self)._init()
        
def setup_logging():
    logging.addLevelName('TRACE1', TRACE1)
    from logging import config
    config.fileConfig(os.path.join(os.environ['ZATO_REPO_LOCATION'], 'logging.conf'))

def start_connector(repo_location, file_, env_item_name, def_id, item_id):
    """ Starts a new connector process.
    """
    
    # Believe it or not but this is the only sane way to make connector subprocesses 
    # work as of now (15 XI 2011).
    
    # Subprocesses spawned in a shell need to use
    # the wrapper which sets up the PYTHONPATH instead of the regular Python
    # executable, because the executable may not have all the dependencies required.
    # Of course, this needs to be squared away before Zato gets into any Linux 
    # distribution but then the situation will be much simpler as we simply won't 
    # have to patch up anything, the distro will take care of any dependencies.
    executable = os.path.join(os.path.dirname(sys.executable), 'py')
    
    if file_[-1] in('c', 'o'): # Need to use the source code file
        file_ = file_[:-1]
    
    program = '{0} {1}'.format(executable, file_)
    
    zato_env = {}
    zato_env['ZATO_REPO_LOCATION'] = repo_location
    if def_id:
        zato_env['ZATO_CONNECTOR_DEF_ID'] = str(def_id)
    zato_env[env_item_name] = str(item_id)
    
    _env = os.environ
    _env.update(zato_env)
    
    Popen(program, close_fds=True, shell=True, env=_env)
    
