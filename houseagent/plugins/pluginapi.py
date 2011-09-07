from twisted.internet.defer import inlineCallbacks
from twisted.internet.error import ConnectionRefusedError
from twisted.internet.protocol import ClientCreator
from twisted.python import log
from txamqp.client import TwistedDelegate
from txamqp.content import Content
from txamqp.protocol import AMQClient
from txamqp.queue import Closed
from twisted.python import log as twisted_log
import logging, logging.handlers
import sys
import txamqp.spec
import os
import json
import time
if os.name == "nt":
    from twisted.internet import win32eventreactor
    win32eventreactor.install()
from twisted.internet import reactor, task
from houseagent import log_path

class PluginAPI(object):
    '''
    This is the PluginAPI for HouseAgent, it allows you to create a connection to the broker.
    ''' 
    def __init__(self, plugin_id=None, plugin_type=None, broker_ip='127.0.0.1', broker_port=5672, username='guest', password='guest', vhost='/', logging=False):
        
        self._broker_host = broker_ip
        self._broker_port = broker_port
        self._broker_user = username
        self._broker_pass = password
        self._broker_vhost = vhost
        log.msgging = logging

        self._qname = plugin_id
        self._plugintype = plugin_type
        self._tag = 'mq%d' % id(self)
        
        if logging:
            log.startLogging(sys.stdout)            
            log.startLogging(open(plugin_id + '.log', 'w+'))
       
        self._connect_client()

    @inlineCallbacks
    def _connect_client(self):
        '''
        Sets up a client connection to the RabbitMQ broker.
        '''
        try:
            spec = txamqp.spec.load("../../specs/amqp0-8.xml")
        except: 
            spec = txamqp.spec.load("amqp0-8.xml")
            
        try:
            client = yield ClientCreator(reactor, AMQClient, TwistedDelegate(), self._broker_vhost, spec).connectTCP(self._broker_host, int(self._broker_port))
        except ConnectionRefusedError:            
            log.msg("Failed to connect to RabbitMQ broker.. retrying..")
            reactor.callLater(10.0, self._connect_client)
            return
        except Exception, e:
            log.msg("Unhandled exception while connecting to RabbitMQ broker: %s" % e)
          
        log.msg("Connected to RabbitMQ broker, authenticating...")
        yield client.authenticate(self._broker_user, self._broker_pass)
        self._setup(client)
    
    @inlineCallbacks
    def _setup(self, client):
        self._client = client
        
        try:
            self._channel = yield self._client.channel(1)
        except:
            log.msg("Error setting up RabbitMQ communication channel!")      

        try:
            yield self._channel.channel_open()
        except:
            log.msg("Error opening RabbitMQ communication channel!")
                        
        # Declare exchange
        try:
            yield self._channel.exchange_declare(exchange="houseagent.direct", type="direct", durable="True")
        except:
            log.msg("Error declaring RabbitMQ exchange!")

        # Declare queue
        try:
            yield self._channel.queue_declare(queue=self._qname, durable=True, auto_delete=True)
        except:
            log.msg("Error declaring RabbitMQ queue!")

        # Bind queue
        try:
            yield self._channel.queue_bind(queue=self._qname, exchange="houseagent.direct",
                                     routing_key=self._qname)
        except:
            log.msg("Error binding RabbitMQ queue's!")

        # Set-up consumer
        try:
            self._channel.basic_consume(queue=self._qname, no_ack=True,
                                        consumer_tag=self._tag)
        except:
            log.msg("Error setting up RabbitMQ consumer!")

        # Start receiving message from the broker
        log.msg("Succesfully setup RabbitMQ broker connection...")
        self._client.queue(self._tag).addCallback(lambda queue: self.handle_msg(None, queue))                    
        
        # This checks all plugins every 10 seconds
        l = task.LoopingCall(self._ping)
        l.start(10.0)
    
    def handle_err(self, failure, queue):
        '''
        This handles message get errors. 
        '''
        if failure.check(Closed):
            self._connect_client()
        else:
            print 'error: %s' % failure
            self.handle_msg(None, queue)

    def setup_error(self, failure):
        print 'ERROR: failed to create RPC Receiver: %s' % failure
    
    def handle_msg(self, msg, queue):
        d = queue.get()
        d.addCallback(self.handle_msg, queue)
        d.addErrback(self.handle_err, queue)

        if msg:
            print "received message", msg
            replyq = msg.content.properties.get('reply to',None)
            
            if msg.content and replyq:
                request = json.loads(msg.content.body)
                
                print "received custom request", request
                
                if request["type"] == "custom":
                    result = self.customcallback.on_custom(request["action"], request["parameters"])
                    
                    content = Content(json.dumps(result))
                    content.properties['correlation id'] = msg.content.properties['correlation id']
                    self._channel.basic_publish(exchange="", content=content, routing_key="houseagent")
                elif request["type"] == "poweron":
                    print "POWERON"
                    result = self.poweroncallback.on_poweron(request["address"])
                    
                    content = Content(json.dumps(result))
                    content.properties['correlation id'] = msg.content.properties['correlation id']
                    self._channel.basic_publish(exchange="", content=content, routing_key="houseagent")      
                elif request["type"] == "poweroff":
                    result = self.poweroncallback.on_poweroff(request["address"])
                    
                    content = Content(json.dumps(result))
                    content.properties['correlation id'] = msg.content.properties['correlation id']
                    self._channel.basic_publish(exchange="", content=content, routing_key="houseagent")         
                elif request["type"] == "dim":
                    result = self.dimcallback.on_dim(request["address"], request["level"])
                    
                    content = Content(json.dumps(result))
                    content.properties['correlation id'] = msg.content.properties['correlation id']
                    self._channel.basic_publish(exchange="", content=content, routing_key="houseagent")         
                elif request["type"] == "thermostat_setpoint":
                    result = self.thermostatcallback.on_thermostat_setpoint(request['address'], request['temperature'])
                    
                    content = Content(json.dumps(result))
                    content.properties['correlation id'] = msg.content.properties['correlation id']
                    self._channel.basic_publish(exchange="", content=content, routing_key="houseagent")         
                
    def register_custom(self, calling_class):
        """
        Register's for a custom command callback.
        """
        self.customcallback = calling_class
        
    def register_poweron(self, calling_class):
        self.poweroncallback = calling_class
        
    def register_poweroff(self, calling_class):
        self.poweroffcallback = calling_class

    def register_dim(self, calling_class):
        self.dimcallback = calling_class
        
    def register_thermostat_setpoint(self, calling_class):
        self.thermostatcallback = calling_class
            
    def value_update(self, address, values):
        '''
        Called by a plugin when a device value has been updated.
        '''
        content = {"address": address,
                   "values": values, 
                   "time": time.time(),
                   "plugin_id": self._qname}
        
        msg = Content(json.dumps(content))
        msg["delivery mode"] = 2
        self._channel.basic_publish(exchange="houseagent.direct", content=msg, routing_key="value_updates")
        print "Sending message: %s" % content
        
    def _ping(self):
        '''
        Sends an alive message on the network.
        '''
        content = {"id": self._qname,
                   "type": self._plugintype}
        
        msg = Content(json.dumps(content))
        msg["delivery mode"] = 1
        self._channel.basic_publish(exchange="houseagent.direct", content=msg, routing_key="network")
        
class Logging():
    '''
    This class provides generic logging facilities for HouseAgent plug-ins. 
    '''
    
    def __init__(self, name, maxkbytes=1024, count=5, console=True):
        '''
        Using this class you can add logging to your plug-in.
        It provides a generic way of storing logging information.
        
        @param name: the name of the logfile 
        @param maxkbytes: the maximum logfile size in kilobytes 
        @param count: the maximum number of logfiles to keep for rotation
        @param console: specifies whether or not to log to the console, this defaults to "True"
        '''
        
        # Start Twisted python log observer
        observer = twisted_log.PythonLoggingObserver()
        observer.start()
        
        # Regular Python logging module
        self.logger = logging.getLogger()
        log_handler = logging.handlers.RotatingFileHandler(filename = os.path.join(log_path, "%s.log" % name), 
                                                       maxBytes = maxkbytes * 1024,
                                                       backupCount = count)
        
        formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s')
        
        if console:
            console_handler = logging.StreamHandler(sys.stdout) 
            console_handler.setFormatter(formatter)
        
        log_handler.setFormatter(formatter)
        
        self.logger.addHandler(log_handler)
        self.logger.addHandler(console_handler)
        
    def set_level(self, level):        
        '''
        This function allows you to set the level of logging.
        By default everything will be logged. 
        @param level: the level of logging, valid arguments are debug, warning or error.
        '''
        if level == 'debug':
            self.logger.setLevel(logging.DEBUG)
        elif level == 'warning':
            self.logger.setLevel(logging.WARNING)
        elif level == 'error':
            self.logger.setLevel(logging.ERROR)
            
    def error(self, message):
        '''
        This function allows you to log a plugin error message.
        @param message: the message to log.
        '''
        twisted_log.msg(message, logLevel=logging.ERROR)
        
    def warning(self, message):
        '''
        This function allows you to log a plugin warning message.
        @param message: the message to log.
        '''
        twisted_log.msg(message, logLevel=logging.WARNING)
    
    def debug(self, message):
        '''
        This function allows you to log a plugin debug message.
        @param message: the message to log.
        '''        
        twisted_log.msg(message, logLevel=logging.DEBUG)