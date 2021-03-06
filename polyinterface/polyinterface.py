#!/usr/bin/env python
"""
Python Interface for UDI Polyglot v2 NodeServers
by Einstein.42 (James Milne) milne.james@gmail.com
"""

import warnings
from copy import deepcopy
# from dotenv import load_dotenv
import json
import ssl
import logging
import __main__ as main
import markdown2
import os
from os.path import join, expanduser
import paho.mqtt.client as mqtt
try:
    import queue
except ImportError:
    import Queue as queue
import re
import sys
import select
import base64
import random
import string
from threading import Thread, current_thread
import time
import netifaces
from .polylogger import LOGGER

DEBUG = False
PY2 = sys.version_info[0] == 2

if PY2:
    string_types = basestring
else:
    string_types = str


class LoggerWriter(object):
    def __init__(self, level):
        self.level = level

    def write(self, message):
        if isinstance(message, string_types):
            # It's a string !!
            if not re.match(r'^\s*$', message):
                self.level(message.strip())
        else:
            self.level('ERROR: message was not a string: {}'.format(message))

    def flush(self):
        pass


def get_network_interface(interface='default'):
    """
    Returns the network interface which contains addr, broadcasts, and netmask elements

    :param interface: The interface name to check, default grabs
    """
    # Get the default gateway
    gws = netifaces.gateways()
    LOGGER.debug("gws: {}".format(gws))
    rt = False
    if interface in gws:
        gwd = gws[interface][netifaces.AF_INET]
        LOGGER.debug("gw: {}={}".format(interface, gwd))
        ifad = netifaces.ifaddresses(gwd[1])
        rt = ifad[netifaces.AF_INET]
        LOGGER.debug("ifad: {}={}".format(gwd[1], rt))
        return rt[0]
    LOGGER.error("No {} in gateways:{}".format(interface, gws))
    return {'addr': False, 'broadcast': False, 'netmask': False}


def random_string(length):
    letters_and_digits = string.ascii_letters + string.digits
    result_str = ''.join((random.choice(letters_and_digits)
                          for i in range(length)))
    return result_str


def init_interface():
    sys.stdout = LoggerWriter(LOGGER.debug)
    sys.stderr = LoggerWriter(LOGGER.error)

    """
    Grab the ~/.polyglot/.env file for variables
    If you are running Polyglot v2 on this same machine
    then it should already exist. If not create it.
    """
    # warnings.simplefilter('error', UserWarning)
    # try:
    #     load_dotenv(join(expanduser("~") + '/.polyglot/.env'))
    # except (UserWarning) as err:
    #     LOGGER.warning('File does not exist: {}.'.format(
    #         join(expanduser("~") + '/.polyglot/.env')), exc_info=True)
    #     # sys.exit(1)
    # warnings.resetwarnings()

    """
    If this NodeServer is co-resident with Polyglot it will receive a STDIN config on startup
    that looks like:
    {"token":"2cb40e507253fc8f4cbbe247089b28db79d859cbed700ec151",
    "mqttHost":"localhost","mqttPort":"1883","profileNum":"10"}
    """

    # init = select.select([sys.stdin], [], [], 1)[0]
    # if init:
    #     line = sys.stdin.readline()
    #     try:
    #         line = json.loads(line)
    #         os.environ['PROFILE_NUM'] = line['profileNum']
    #         os.environ['MQTT_HOST'] = line['mqttHost']
    #         os.environ['MQTT_PORT'] = line['mqttPort']
    #         os.environ['TOKEN'] = line['token']
    #         LOGGER.info('Received Config from STDIN.')
    #     except (Exception) as err:
    #         # e = sys.exc_info()[0]
    #         LOGGER.error('Invalid formatted input %s for line: %s',
    #                      line, err, exc_info=True)


def unload_interface():
    sys.stdout = sys.__stdout__
    sys.stderr = sys.__stderr__
    LOGGER.handlers = []


class Interface(object):

    CUSTOM_CONFIG_DOCS_FILE_NAME = 'POLYGLOT_CONFIG.md'
    SERVER_JSON_FILE_NAME = 'server.json'

    """
    Polyglot Interface Class

    :param envVar: The Name of the variable from ~/.polyglot/.env that has this NodeServer's profile number
    """
    # pylint: disable=too-many-instance-attributes
    # pylint: disable=unused-argument

    __exists = False

    def __init__(self, envVar=None):
        if self.__exists:
            warnings.warn('Only one Interface is allowed.')
            return
        try:
            self.pg3init = json.loads(
                base64.b64decode(os.environ.get('PG3INIT')))
        except:
            LOGGER.error('Failed to parse init. Exiting...',exc_info=True)
            sys.exit(1)
        self.config = None
        self.connected = False
        self.uuid = self.pg3init['uuid']
        self.profileNum = str(self.pg3init['profileNum'])
        self.id = '{}_{}'.format(self.uuid, self.profileNum)
        self.topicInput = 'udi/pg3/ns/clients/{}'.format(self.id)
        self._threads = {}
        self._threads['socket'] = Thread(
            target=self._startMqtt, name='Interface')
        self._mqttc = mqtt.Client(self.id, True)
        self._mqttc.username_pw_set(self.id, self.pg3init['token'])
        self._mqttc.on_connect = self._connect
        self._mqttc.on_message = self._message
        self._mqttc.on_subscribe = self._subscribe
        self._mqttc.on_disconnect = self._disconnect
        self._mqttc.on_publish = self._publish
        self._mqttc.on_log = self._log
        self.useSecure = True
        self.custom = {}
        if self.pg3init['secure'] is 1:
            self.sslContext = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
            self.sslContext.check_hostname = False
        self._mqttc.tls_set_context(self.sslContext)
        self.loop = None
        self.inQueue = queue.Queue()
        # self.thread = Thread(target=self.start_loop)
        self.isyVersion = None
        self._server = self.pg3init['mqttHost'] or 'localhost'
        self._port = self.pg3init['mqttPort'] or '1883'
        self.polyglotConnected = False
        self.__configObservers = []
        self.__stopObservers = []
        Interface.__exists = True
        self.custom_params_docs_file_sent = False
        self.custom_params_pending_docs = ''
        self.currentLogLevel = ''
        try:
            self.network_interface = self.get_network_interface()
            LOGGER.info('Connect: Network Interface: {}'.format(
                self.network_interface))
        except:
            self.network_interface = False
            LOGGER.error(
                'Failed to determine Network Interface', exc_info=True)

    def onConfig(self, callback):
        """
        Gives the ability to bind any methods to be run when the config is received.
        """
        self.__configObservers.append(callback)

    def onStop(self, callback):
        """
        Gives the ability to bind any methods to be run when the stop command is received.
        """
        self.__stopObservers.append(callback)

    def _connect(self, mqttc, userdata, flags, rc):
        """
        The callback for when the client receives a CONNACK response from the server.
        Subscribing in on_connect() means that if we lose the connection and
        reconnect then subscriptions will be renewed.

        :param mqttc: The client instance for this callback
        :param userdata: The private userdata for the mqtt client. Not used in Polyglot
        :param flags: The flags set on the connection.
        :param rc: Result code of connection, 0 = Success, anything else is a failure
        """
        if current_thread().name != "MQTT":
            current_thread().name = "MQTT"
        if rc == 0:
            self.connected = True
            results = []
            LOGGER.info("MQTT Connected with result code " +
                        str(rc) + " (Success)")
            # result, mid = self._mqttc.subscribe(self.topicInput)
            results.append((self.topicInput, tuple(
                self._mqttc.subscribe(self.topicInput))))
            # results.append((self.topicPolyglotConnection, tuple(self._mqttc.subscribe(self.topicPolyglotConnection))))
            for (topic, (result, mid)) in results:
                if result == 0:
                    LOGGER.info("MQTT Subscribing to topic: " + topic +
                                " - " + " MID: " + str(mid) + " Result: " + str(result))
                else:
                    LOGGER.info("MQTT Subscription to " + topic +
                                " failed. This is unusual. MID: " + str(mid) + " Result: " + str(result))
                    # If subscription fails, try to reconnect.
                    self._mqttc.reconnect()
            self.send({'getAll': {}}, 'custom')
        else:
            LOGGER.error("MQTT Failed to connect. Result code: " + str(rc))

    def _message(self, mqttc, userdata, msg):
        """
        The callback for when a PUBLISH message is received from the server.

        :param mqttc: The client instance for this callback
        :param userdata: The private userdata for the mqtt client. Not used in Polyglot
        :param flags: The flags set on the connection.
        :param msg: Dictionary of MQTT received message. Uses: msg.topic, msg.qos, msg.payload
        """
        try:
            inputCmds = ['query', 'command', 'addnode',
                         'status', 'shortPoll', 'longPoll', 'delete',
                         'setLogLevel']
            parsed_msg = json.loads(msg.payload.decode('utf-8'))
            if DEBUG:
                LOGGER.debug('MQTT Received Message: {}: {}'.format(
                    msg.topic, parsed_msg))
            for key in parsed_msg:
                if DEBUG:
                    LOGGER.debug('MQTT Processing Message: {}: {}'.format(
                        msg.topic, parsed_msg))
                if key == 'config':
                    self.inConfig(parsed_msg[key])
                elif key == 'stop':
                    LOGGER.debug(
                        'Received stop from Polyglot... Shutting Down.')
                    self.stop()
                elif key == 'setLogLevel':
                    try:
                        LOGGER.setLevel(parsed_msg[key]['level'].upper())
                        self.currentLogLevel = parsed_msg[key]['level'].upper()
                    except (KeyError, ValueError) as err:
                        LOGGER.error('handleInput: {}'.format(err), exc_info=True)
                elif key == 'set':
                    if isinstance(parsed_msg[key], list):
                        for item in parsed_msg[key]:
                            if item.get('address') is not None:
                                LOGGER.info('Successfully set {} :: {} to {} UOM {}'.format(
                                    item.get('address'), item.get('driver'), item.get('value'), item.get('uom')))
                            elif item.get('success'):
                                if item.get('success') is True:
                                    for type in item:
                                        if type != 'success':
                                            LOGGER.info(
                                                'Successfully set {}'.format(type))
                                else:
                                    for type in item:
                                        if type != 'success':
                                            LOGGER.error(
                                                'Failed to set {} :: Error: {}'.format(type, ))

                    else:
                        LOGGER.error('set input was not a list')
                elif key == 'getAll':
                    if isinstance(parsed_msg[key], list):
                        for custom in parsed_msg[key]:
                            LOGGER.debug(
                                'Received {} from database'.format(custom.get('key')))
                            try:
                                value = json.loads(custom.get('value'))
                                self.custom[custom.get('key')] = value
                            except ValueError as e:
                                self.custom[custom.get(
                                    'key')] = custom.get('value')
                    if self.config is None:
                        self.send({'config': {}}, 'system')
                elif key in inputCmds:
                    self.input(parsed_msg)
                else:
                    LOGGER.error(
                        'Invalid command received in message from PG3: {}'.format(key))
        except (ValueError) as err:
            LOGGER.error('MQTT Received Payload Error: {}'.format(
                err), exc_info=True)
        except Exception as ex:
            # Can any other exception happen?
            template = "An exception of type {0} occured. Arguments:\n{1!r}"
            message = template.format(type(ex).__name__, ex.args)
            LOGGER.error("MQTT Received Unknown Error: " +
                         message, exc_info=True)

    def _disconnect(self, mqttc, userdata, rc):
        """
        The callback for when a DISCONNECT occurs.

        :param mqttc: The client instance for this callback
        :param userdata: The private userdata for the mqtt client. Not used in Polyglot
        :param rc: Result code of connection, 0 = Graceful, anything else is unclean
        """
        self.connected = False
        if rc != 0:
            LOGGER.info(
                "MQTT Unexpected disconnection. Trying reconnect. rc: {}".format(rc))
            try:
                self._mqttc.reconnect()
            except Exception as ex:
                template = "An exception of type {0} occured. Arguments:\n{1!r}"
                message = template.format(type(ex).__name__, ex.args)
                LOGGER.error("MQTT Connection error: " + message)
        else:
            LOGGER.info("MQTT Graceful disconnection.")

    def _log(self, mqttc, userdata, level, string):
        """ Use for debugging MQTT Packets, disable for normal use, NOISY. """
        if DEBUG:
            LOGGER.info('MQTT Log - {}: {}'.format(str(level), str(string)))

    def _subscribe(self, mqttc, userdata, mid, granted_qos):
        """ Callback for Subscribe message. Unused currently. """
        LOGGER.info(
            "MQTT Subscribed Succesfully for Message ID: {} - QoS: {}".format(str(mid), str(granted_qos)))

    def _publish(self, mqttc, userdata, mid):
        """ Callback for publish message. Unused currently. """
        if DEBUG:
            LOGGER.info("MQTT Published message ID: {}".format(str(mid)))

    def start(self):
        for _, thread in self._threads.items():
            thread.start()

    def _startMqtt(self):
        """
        The client start method. Starts the thread for the MQTT Client
        and publishes the connected message.
        """
        LOGGER.info('Connecting to MQTT... {}:{}'.format(
            self._server, self._port))
        done = False
        while not done:
            try:
                # self._mqttc.connect_async(str(self._server), int(self._port), 10)
                self._mqttc.connect_async('{}'.format(
                    self._server), int(self._port), 10)
                self._mqttc.loop_forever()
                done = True
            except ssl.SSLError as e:
                LOGGER.error("MQTT Connection SSLError: {}, Will retry in a few seconds.".format(
                    e), exc_info=True)
                time.sleep(3)
            except Exception as ex:
                template = "An exception of type {0} occurred. Arguments:\n{1!r}"
                message = template.format(type(ex).__name__, ex.args)
                LOGGER.error("MQTT Connection error: {}".format(
                    message), exc_info=True)
                done = True
        LOGGER.debug("MQTT: Done")

    def stop(self):
        """
        The client stop method. If the client is currently connected
        stop the thread and disconnect. Publish the disconnected
        message if clean shutdown.
        """
        # self.loop.call_soon_threadsafe(self.loop.stop)
        # self.loop.stop()
        # self._longPoll.cancel()
        # self._shortPoll.cancel()
        if self.connected:
            LOGGER.info('Disconnecting from MQTT... {}:{}'.format(
                self._server, self._port))
            # self._mqttc.publish(self.topicSelfConnection, json.dumps({'node': self.profileNum, 'connected': False}), retain=True)
            self._mqttc.loop_stop()
            self._mqttc.disconnect()
        try:
            for watcher in self.__stopObservers:
                watcher()
        except KeyError as e:
            LOGGER.exception(
                'KeyError in stop: {}'.format(e), exc_info=True)

    def send(self, message, type):
        """
        Formatted Message to send to Polyglot. Connection messages are sent automatically from this module
        so this method is used to send commands to/from Polyglot and formats it for consumption
        """
        if not isinstance(message, dict) and self.connected:
            warnings.warn('payload not a dictionary')
            return False
        try:
            # message['node'] = self.profileNum
            validTypes = ['status', 'command', 'system', 'custom']
            if not type in validTypes:
                warnings.warn('send: type not valid')
                return False
            topic = 'udi/pg3/ns/{}/{}'.format(type, self.id)
            self._mqttc.publish(topic, json.dumps(message), retain=False)
        except TypeError as err:
            LOGGER.error('MQTT Send Error: {}'.format(err), exc_info=True)

    def addNode(self, node):
        """
        Add a node to the NodeServer

        :param node: Dictionary of node settings. Keys: address, name, node_def_id, primary, and drivers are required.
        """
        LOGGER.info('Adding node {}({})'.format(node.name, node.address))
        message = {
            'addnode': [{
                'address': node.address,
                'name': node.name,
                'nodeDefId': node.id,
                'primaryNode': node.primary,
                'drivers': node.drivers,
                'hint': node.hint
            }]
        }
        self.send(message, 'command')

    def saveCustom(self, key):
        """
        Send custom dictionary to Polyglot to save and be retrieved on startup.

        :param key: Dictionary of key value pairs to store in Polyglot database.
        """
        LOGGER.info('Sending custom {} to Polyglot.'.format(key))
        message = {'set': [{'key': key, 'value': self.custom[key]}]}
        self.send(message, 'custom')

    # def saveCustomParams(self, data):
    #     """
    #     Send custom dictionary to Polyglot to save and be retrieved on startup.

    #     :param data: Dictionary of key value pairs to store in Polyglot database.
    #     """
    #     LOGGER.info('Sending customParams to Polyglot.')
    #     message = {'set': [{'key': 'customparams', 'value': data}]}
    #     self.send(message, 'custom')

    # def updateNotices(self):
    #     """
    #     Add custom notice to front-end for this NodeServers

    #     :param data: String of characters to add as a notification in the front-end.
    #     """
    #     LOGGER.info('Updating Notices on PG3')
    #     message = {'set': [{'key': 'notices', 'value': self.notices}]}
    #     self.send(message, 'custom')

    def restart(self):
        """
        Send a command to Polyglot to restart this NodeServer
        """
        LOGGER.info('Asking Polyglot to restart me.')
        message = {
            'restart': {}
        }
        self.send(message, 'system')

    def installprofile(self):
        LOGGER.info('Sending Install Profile command to Polyglot.')
        message = {'installprofile': {'reboot': False}}
        self.send(message, 'system')

    def delNode(self, address):
        """
        Delete a node from the NodeServer

        :param node: Dictionary of node settings. Keys: address, name, node_def_id, primary, and drivers are required.
        """
        LOGGER.info('Removing node {}'.format(address))
        message = {
            'removenode': {
                'address': address
            }
        }
        self.send(message, 'command')

    def getNode(self, address):
        """
        Get Node by Address of existing nodes.
        """
        try:
            for node in self.config['nodes']:
                if node['address'] == address:
                    return node
            return False
        except KeyError:
            LOGGER.error(
                'Usually means we have not received the config yet.', exc_info=True)
            return False

    def inConfig(self, config):
        """
        Save incoming config received from Polyglot to Interface.config and then do any functions
        that are waiting on the config to be received.
        """
        self.config = config
        # self.isyVersion = config['isyVersion']

        """ is log level in here? """
        if 'logLevel' in config:
            self.currentLogLevel = config['logLevel']

        try:
            for watcher in self.__configObservers:
                watcher(config)

            self.send_custom_config_docs()

        except KeyError as e:
            LOGGER.error('KeyError in gotConfig: {}'.format(e), exc_info=True)

    def input(self, command):
        self.inQueue.put(command)

    def supports_feature(self, feature):
        return True

    def getLogLevel(self):
        return self.currentLogLevel

    def setLogLevel(self, newLevel):
        LOGGER.info('Setting log level to {}'.format(newLevel))
        message = {
            'setLogLevel': { 'level': newLevel.upper() }
        }
        self.send(message, 'system')

    def get_md_file_data(self, fileName):
        data = ''
        if os.path.isfile(fileName):
            data = markdown2.markdown_path(fileName)

        return data

    def send_custom_config_docs(self):
        data = ''
        if not self.custom_params_docs_file_sent:
            data = self.get_md_file_data(
                Interface.CUSTOM_CONFIG_DOCS_FILE_NAME)
        else:
            data = self.custom.get('customparamsdoc', '')

        # send if we're sending new file or there are updates
        if (not self.custom_params_docs_file_sent or
                len(self.custom_params_pending_docs) > 0):
            data += self.custom_params_pending_docs
            self.custom_params_docs_file_sent = True
            self.custom_params_pending_docs = ''

            self.custom['customparamsdoc'] = data
            self.saveCustom('customparamsdoc')

    def add_custom_config_docs(self, data, clearCurrentData=False):
        if clearCurrentData:
            self.custom_params_docs_file_sent = False

        self.custom_params_pending_docs += data
        self.send_custom_config_docs()

    def save_typed_params(self, data):
        """
        Send custom parameters descriptions to Polyglot to be used
        in front end UI configuration screen
        Accepts list of objects with the followin properties
            name - used as a key when data is sent from UI
            title - displayed in UI
            defaultValue - optionanl
            type - optional, can be 'NUMBER', 'STRING' or 'BOOLEAN'.
                Defaults to 'STRING'
            desc - optional, shown in tooltip in UI
            isRequired - optional, True/False, when set, will not validate UI
                input if it's empty
            isList - optional, True/False, if set this will be treated as list
                of values or objects by UI
            params - optional, can contain a list of objects. If present, then
                this (parent) is treated as object / list of objects by UI,
                otherwise, it's treated as a single / list of single values
        """
        LOGGER.info('Sending typed parameters to Polyglot.')
        if type(data) is not list:
            data = [data]
        self.custom['customtypedparams'] = data
        self.saveCustom('customtypedparams')

    def get_network_interface(self, interface='default'):
        return get_network_interface(interface=interface)

    def get_server_data(self, check_profile=True, build_profile=None):
        """
        get_server_data: Loads the server.json and returns as a dict
        :param check_profile: Calls the check_profile method if True

        If profile_version in json is null then profile will be loaded on
        every restart.

        """
        serverdata = {'version': 'unknown'}
        # Read the SERVER info from the json.
        try:
            with open(Interface.SERVER_JSON_FILE_NAME) as data:
                serverdata = json.load(data)
        except Exception as err:
            LOGGER.error('get_server_data: failed to read file {0}: {1}'.format(
                Interface.SERVER_JSON_FILE_NAME, err), exc_info=True)
            return serverdata
        data.close()
        # Get the version info
        try:
            version = serverdata['credits'][0]['version']
        except (KeyError, ValueError):
            LOGGER.info(
                'Version (credits[0][version]) not found in server.json.')
            version = '0.0.0.0'
        serverdata['version'] = version
        if not 'profile_version' in serverdata:
            serverdata['profile_version'] = "NotDefined"
        LOGGER.debug('get_server_data: {}'.format(serverdata))
        if check_profile:
            force = True if serverdata['profile_version'] is None else False
            self.check_profile(serverdata, force=force,
                               build_profile=build_profile)
        return serverdata

    def check_profile(self, serverdata, force=False, build_profile=None):
        """
        Check if the profile is up to date by comparing the server.json profile_version
        against the profile_version stored in the db customdata
        The profile will be installed if necessary.
        """
        LOGGER.debug('check_profile: force={} build_profile={}'.format(
            force, build_profile))
        cdata = deepcopy(self.custom.get('customdata')) or {}
        LOGGER.debug('check_profile:      customdata={}'.format(cdata))
        LOGGER.debug('check_profile: profile_version={}'.format(
            serverdata['profile_version']))
        if serverdata['profile_version'] == "NotDefined":
            LOGGER.error(
                'check_profile: Ignoring since nodeserver does not have profile_version')
            return
        update_profile = False
        if force:
            LOGGER.warning('check_profile: Force is enabled.')
            update_profile = True
        elif not 'profile_version' in cdata:
            LOGGER.info(
                'check_profile: Updated needed since it has never been recorded.')
            update_profile = True
        elif isinstance(cdata, dict) and serverdata['profile_version'] == cdata['profile_version']:
            LOGGER.info('check_profile: No updated needed: "{}" == "{}"'.format(
                serverdata['profile_version'], cdata['profile_version']))
            update_profile = False
        else:
            LOGGER.info('check_profile: Updated needed: "{}" == "{}"'.format(
                serverdata['profile_version'], cdata['profile_version']))
            update_profile = True
        if update_profile:
            if build_profile:
                LOGGER.info('Building Profile...')
                build_profile()
            st = self.installprofile()
            cdata['profile_version'] = serverdata['profile_version']
            self.custom['customdata'] = cdata
            self.saveCustom('customdata')


class Node(object):
    """
    Node Class for individual devices.
    """

    def __init__(self, controller, primary, address, name):
        try:
            self.controller = controller
            self.parent = self.controller
            self.primary = primary
            self.address = address
            self.name = name
            self.polyConfig = None
            self.drivers = deepcopy(self.drivers)
            self._drivers = deepcopy(self.drivers)
            self.isPrimary = None
            self.config = None
            self.timeAdded = None
            self.enabled = None
            self.added = None
        except (KeyError) as err:
            LOGGER.error('Error Creating node: {}'.format(err), exc_info=True)

    def _convertDrivers(self, drivers):
        return deepcopy(drivers)
        """
        if isinstance(drivers, list):
            newFormat = {}
            for driver in drivers:
                newFormat[driver['driver']] = {}
                newFormat[driver['driver']]['value'] = driver['value']
                newFormat[driver['driver']]['uom'] = driver['uom']
            return newFormat
        else:
            return deepcopy(drivers)
        """

    def setDriver(self, driver, value, report=True, force=False, uom=None):
        for d in self.drivers:
            if d['driver'] == driver:
                d['value'] = value
                if uom is not None:
                    d['uom'] = uom
                if report:
                    self.reportDriver(d, report, force)
                break

    def reportDriver(self, driver, report, force):
        for d in self._drivers:
            if (d['driver'] == driver['driver'] and
                (str(d['value']) != str(driver['value']) or
                    d['uom'] != driver['uom'] or
                    force)):
                LOGGER.info('Updating Driver {} - {}: {}, uom: {}'.format(self.address,
                                                                          driver['driver'], driver['value'], driver['uom']))
                d['value'] = deepcopy(driver['value'])
                if d['uom'] != driver['uom']:
                    d['uom'] = deepcopy(driver['uom'])
                message = {
                    'set': [{
                        'address': self.address,
                        'driver': driver['driver'],
                        'value': str(driver['value']),
                        'uom': driver['uom']
                    }]
                }
                self.controller.poly.send(message, 'status')
                break

    def reportCmd(self, command, value=None, uom=None):
        message = {
            'command': [{
                'address': self.address,
                'command': command
            }]
        }
        if value is not None and uom is not None:
            message['command']['value'] = str(value)
            message['command']['uom'] = uom
        self.controller.poly.send(message, 'command')

    def reportDrivers(self):
        LOGGER.info('Updating All Drivers to ISY for {}({})'.format(
            self.name, self.address))
        self.updateDrivers(self.drivers)
        message = {'set': []}
        for driver in self.drivers:
            message['set'].append(
                {
                    'address': self.address,
                    'driver': driver['driver'],
                    'value': driver['value'],
                    'uom': driver['uom']
                })
        self.controller.poly.send(message, 'status')

    def updateDrivers(self, drivers):
        self._drivers = deepcopy(drivers)

    def query(self):
        self.reportDrivers()

    def status(self):
        self.reportDrivers()

    def runCmd(self, command):
        if command['command'] in self.commands:
            fun = self.commands[command['command']]
            fun(self, command)

    def start(self):
        pass

    def getDriver(self, dv):
        for index, node in enumerate(self.controller.poly.config['nodes']):
            LOGGER.debug('{} :: {} :: getting dv {}'.format(index, node, dv))
            if node['address'] == self.address:
                for idx, driver in enumerate(node['drivers']):
                    LOGGER.debug('{} :: {} - {} :: getting dv {}'.format(idx,
                                                                         driver['driver'], driver['value'], dv))
                    if driver['driver'] == dv:
                        return driver['value']
        return None

    def toJSON(self):
        LOGGER.debug(json.dumps(self.__dict__))

    def __rep__(self):
        return self.toJSON()

    id = ''
    commands = {}
    drivers = []
    sends = {}
    hint = [0, 0, 0, 0]


class Controller(Node):
    """
    Controller Class for controller management. Superclass of Node
    """
    __exists = False

    def __init__(self, poly, name='Controller'):
        if self.__exists:
            warnings.warn('Only one Controller is allowed.')
            return
        try:
            self.controller = self
            self.parent = self.controller
            self.poly = poly
            self.poly.onConfig(self._gotConfig)
            self.poly.onStop(self.stop)
            self.name = name
            self.address = 'controller'
            self.primary = self.address
            self._drivers = deepcopy(self.drivers)
            self._nodes = {}
            self.config = None
            self.nodes = {self.address: self}
            self._threads = {}
            self._threads['input'] = Thread(
                target=self._parseInput, name='Controller')
            self._threads['ns'] = Thread(target=self.start, name='NodeServer')
            self.polyConfig = None
            self.isPrimary = None
            self.timeAdded = None
            self.enabled = None
            self.added = None
            self.started = False
            self.nodesAdding = []
            # self._threads = []
            self._startThreads()
        except (KeyError) as err:
            LOGGER.error('Error Creating node: {}'.format(err), exc_info=True)

    def _gotConfig(self, config):
        self.polyConfig = config
        for node in config['nodes']:
            self._nodes[node['address']] = node
            if node['address'] in self.nodes:
                n = self.nodes[node['address']]
                n.updateDrivers(node['drivers'])
                n.config = node
                n.isPrimary = node['isPrimary']
                n.timeAdded = node['timeAdded']
                n.enabled = node['enabled']
                n.added = node['enabled']
        customtypes = ['customparams', 'customdata', 'customparamsdoc',
                       'customtypeddata', 'customtypedparams']
        for type in customtypes:
            if type not in config or config[type] is None:
                config[type] = {}
        if self.address not in self._nodes:
            self.addNode(self)
            LOGGER.info('Waiting on Controller node to be added.......')
        if not self.started:
            self.nodes[self.address] = self
            self.started = True
            # self.setDriver('ST', 1, True, True)
            self._threads['ns'].start()

    def _startThreads(self):
        self._threads['input'].daemon = True
        self._threads['ns'].daemon = True
        self._threads['input'].start()

    def _parseInput(self):
        while True:
            input = self.poly.inQueue.get()
            for key in input:
                if isinstance(input[key], list):
                    for item in input[key]:
                        self._handleInput(key, item)
                else:
                    self._handleInput(key, input[key])
            self.poly.inQueue.task_done()

    def _handleInput(self, key, item):
        if key == 'command':
            if item['address'] in self.nodes:
                try:
                    self.nodes[item['address']].runCmd(item)
                except (Exception) as err:
                    LOGGER.error('_parseInput: failed {}.runCmd({}) {}'.format(
                        item['address'], item['cmd'], err), exc_info=True)
            else:
                LOGGER.error('_parseInput: received command {} for a node that is not in memory: {}'.format(
                    item['cmd'], item['address']))
        elif key == 'addnode':
            self._handleResult(item)
        elif key == 'delete':
            self._delete()
        elif key == 'shortPoll':
            self.shortPoll()
        elif key == 'longPoll':
            self.longPoll()
        elif key == 'query':
            if item['address'] in self.nodes:
                self.nodes[item['address']].query()
            elif item['address'] == 'all':
                self.query()
        elif key == 'status':
            if item['address'] in self.nodes:
                self.nodes[item['address']].status()
            elif item['address'] == 'all':
                self.status()

    def _handleResult(self, result):
        # LOGGER.debug(self.nodesAdding)
        try:
            if result.get('address'):
                if not result.get('address') == self.address:
                    self.nodes.get(result.get('address')).start()
                # self.nodes[result['addnode']['address']].reportDrivers()
                if result.get('address') in self.nodesAdding:
                    self.nodesAdding.remove(result.get('address'))
            else:
                del self.nodes[result.get('address')]
        except (KeyError, ValueError) as err:
            LOGGER.error('handleResult: {}'.format(err), exc_info=True)

    def _delete(self):
        """
        Intermediate message that stops MQTT before sending to overrideable method for delete.
        """
        self.poly.stop()
        self.delete()

    def _convertDrivers(self, drivers):
        return deepcopy(drivers)
        """
        if isinstance(drivers, list):
            newFormat = {}
            for driver in drivers:
                newFormat[driver['driver']] = {}
                newFormat[driver['driver']]['value'] = driver['value']
                newFormat[driver['driver']]['uom'] = driver['uom']
            return newFormat
        else:
            return deepcopy(drivers)
        """

    def delete(self):
        """
        Incoming delete message from Polyglot. This NodeServer is being deleted.
        You have 5 seconds before the process is killed. Cleanup and disconnect.
        """
        pass

    """
    AddNode adds the class to self.nodes then sends the request to Polyglot
    If update is True, overwrite the node in Polyglot
    """

    def addNode(self, node, update=False):
        if node.address in self._nodes:
            node._drivers = self._nodes[node.address]['drivers']
            for driver in node.drivers:
                for existing in self._nodes[node.address]['drivers']:
                    if driver['driver'] == existing['driver']:
                        driver['value'] = existing['value']
                        # JIMBO SAYS NO
                        # driver['uom'] = existing['uom']
        self.nodes[node.address] = node
        # if node.address not in self._nodes or update:
        self.nodesAdding.append(node.address)
        self.poly.addNode(node)
        # else:
        #    self.nodes[node.address].start()
        return node

    """
    Forces a full overwrite of the node
    """

    def updateNode(self, node):
        self.nodes[node.address] = node
        self.nodesAdding.append(node.address)
        self.poly.addNode(node)

    def delNode(self, address):
        """
        Just send it along if requested, should be able to delete the node even if it isn't
        in our config anywhere. Usually used for normalization.
        """
        if address in self.nodes:
            del self.nodes[address]
        self.poly.delNode(address)

    def longPoll(self):
        pass

    def shortPoll(self):
        pass

    def query(self):
        for node in self.nodes:
            self.nodes[node].reportDrivers()

    def status(self):
        for node in self.nodes:
            self.nodes[node].reportDrivers()

    def runForever(self):
        self._threads['input'].join()

    def start(self):
        pass

    def saveCustomData(self, data):
        if not isinstance(data, dict):
            LOGGER.error('saveCustomData: data isn\'t a dictionary. Ignoring.')
        else:
            self.poly.custom['customdata'] = data
            self.poly.saveCustom('customdata')

    def addCustomParam(self, data):
        if not isinstance(data, dict):
            LOGGER.error('addCustomParam: data isn\'t a dictionary. Ignoring.')
        else:
            if self.poly.custom.get('customparams') is not None:
                self.poly.custom['customparams'].update(data)
            else:
                self.poly.custom['customparams'] = data
            self.poly.saveCustom('customparams')

    def removeCustomParam(self, key):
        if not isinstance(key, string_types):
            LOGGER.error('removeCustomParam: key isn\'t a string. Ignoring.')
        else:
            try:
                if self.poly.custom.get('customparams') is not None and isinstance(self.poly.custom['customparams'], dict):
                    self.poly.custom['customparams'].pop(key)
                    self.poly.saveCustom('customparams')
                else:
                    LOGGER.error('removeCustomParam: customparams not found')
            except KeyError:
                LOGGER.error('{} not found in customparams. Ignoring...'.format(
                    key), exc_info=True)

    def getCustomParam(self, key):
        if not isinstance(key, string_types):
            LOGGER.error('getCustomParam: key isn\'t a string. Ignoring.')
        else:
            if self.poly.custom.get('customparams') is not None and isinstance(self.poly.custom['customparams'], dict):
                return self.poly.custom['customparams'].get(key)
            else:
                return None

    def addNotice(self, data, key=None):
        if not isinstance(data, dict):
            LOGGER.error(
                'addNotice: data isn\'t a dictionary. WARNING: DEPRECATED')
            data = {(key if key else random_string(5)): data}
        if self.poly.custom.get('notices') is not None and isinstance(self.poly.custom['notices'], dict):
            self.poly.custom['notices'].update(data)
        else:
            self.poly.custom['notices'] = data
        self.poly.saveCustom('notices')

    def removeNotice(self, key):
        if not isinstance(key, string_types):
            LOGGER.error('removeNotice: key isn\'t a string. Ignoring.')
        else:
            try:
                if self.poly.custom.get('notices') is not None and isinstance(self.poly.custom['customparams'], dict):
                    self.poly.custom['notices'].pop(key)
                    self.poly.saveCustom('notices')
                else:
                    LOGGER.error('removeNotice: notices not found')
            except KeyError:
                LOGGER.error('{} not found in notices. Ignoring...'.format(
                    key), exc_info=True)

    def getNotices(self):
        return self.poly.custom.get('notices')

    def removeNoticesAll(self):
        LOGGER.info('Removing all notices')
        self.poly.custom['notices'] = {}
        self.poly.saveCustom('notices')

    def stop(self):
        """ Called on nodeserver stop """
        pass

    id = 'controller'
    commands = {}
    drivers = [{'driver': 'ST', 'value': 0, 'uom': 2}]


if __name__ == "__main__":
    sys.exit(0)

if hasattr(main, '__file__'):
    init_interface()
