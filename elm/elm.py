#!/usr/bin/env python3
# -*- coding: utf-8 -*-
###########################################################################
# ELM327-emulator
# ELM327 Emulator for testing software interfacing OBDII via ELM327 adapter
# https://github.com/Ircama/ELM327-emulator
# (C) Ircama 2021 - CC-BY-NC-SA-4.0
###########################################################################

import logging
import logging.config
from pathlib import Path
import yaml
import re
import os
import socket
import serial
if not os.name == 'nt':
    import pty
import threading
import time
import sys
import traceback
import errno
from random import randint, choice
from .obd_message import ObdMessage
from .obd_message import ELM_R_OK, ELM_R_UNKNOWN, ST
from .obd_message import ECU_ADDR_E, ECU_R_ADDR_E, ECU_ADDR_I, ECU_R_ADDR_I
from .__version__ import __version__
from functools import reduce
import string
import xml.etree.ElementTree as ET
import importlib
import pkgutil
import inspect

FORWARD_READ_TIMEOUT = 0.2 # seconds
SERIAL_BAUDRATE = 38400 # bps
NETWORK_INTERFACES = ""
PLUGIN_DIR = __package__ + ".plugins"

def setup_logging(
        default_path=Path(__file__).stem + '.yaml',
        default_level=logging.INFO,
        env_key=os.path.basename(Path(__file__).stem).upper() + '_LOG_CFG'):
    path = default_path
    if not os.path.exists(path):
        path = os.path.join(
            os.path.dirname(Path(__file__)), 'elm.yaml')
    value = os.getenv(env_key, None)
    if value:
        path = value
    if os.path.exists(path):
        with open(path, 'rt') as f:
            config = yaml.safe_load(f.read())
        logging.config.dictConfig(config)
    else:
        logging.basicConfig(level=default_level)


class Tasks:
    """
    Base class for tasks.
    All tasks/plugins shall implement a class named Task derived from Tasks.
    """

    class TASK:
        """
        method return values
        """
        TERMINATE = False
        CONTINUE = True
        ERROR = (None, TERMINATE, None)
        INCOMPLETE = (None, CONTINUE, None)
        def PASSTHROUGH(cmd): return (None, Tasks.TASK.TERMINATE, cmd)

    def __init__(self, emulator, header, request, attrib, do_write=False):
        self.emulator = emulator # reference to the emulator namespace
        self.header = header # request header
        self.request = request # original request data (stored before running the start() method)
        self.logging = emulator.logger # logger reference
        self.attrib = attrib # dictionary element
        self.do_write = do_write # (boolean) will write to the application
        self.time_started = time.time() # timer (to be used to simulate background processing)
        self.frame = None # multiline request frame counter
        self.length = None # multiline request length counter
        self.flow_control = 0 # multiline request flow control
        self.flow_control_end = 0x20 # multiline request flow control repetitions

    def HD(self, header):
        """
        Generates the XML tag related to the header byte of the response (EQU ID)
        :param size: header (ECU ID)
        :return: XML tag related to the header of the response
        """
        return ('<header>' + header + '</header>')

    def SZ(self, size):
        """
        Generates the XML tag related to the size byte of the response
        :param size: string including the size byte
        :return: XML tag related to the size byte of the response
        """
        return ('<size>' + size + '</size>')

    def DT(self, data):
        """
        Generates the XML tag related to the data part of the response
        :param data: data part (string of hex data spaced every two bytes)
        :return: XML tag related to the data part of the response
        """
        return ('<data>' + data + '</data>')

    def AW(self, answer):
        """
        Generates the XML tag related to the response, which will be
        automatically translated in header, size and data.
        :param answer: data part (string of hex data)
        :return: XML tag related to the response
        """
        return ('<answer>' + answer + '</answer>')

    def PA(self, pos_answer):
        """
        Generates a positive answer XML tag, including header, size and data.
        :param answer: data part (string of hex data)
        :return: XML tag related to the response
        """
        return ('<pos_answer>' + pos_answer + '</pos_answer>')

    def NA(self, neg_answer):
        """
        Generates a negative answer XML tag, including header, size and data.
        :param answer: data part (string of hex data)
        :return: XML tag related to the response
        """
        return ('<neg_answer>' + neg_answer + '</neg_answer>')

    def task_get_request(self):
        """
        Get the original request command that initiated the task (used to
        generate the answers)
        :return: return the original request string
        """
        return self.request

    def task_request_matched(self, request):
        """
        Check whether the request in the argument matches the original request
        that invoked the task.
        :param request:
        :return: boolean (true if the given request matches the original task request)
        """
        return re.match(self.attrib['Request'], request)

    def start(self, cmd, length=None, frame=None):
        """
        This method is executed when the task is started.
        If not overridden, it calls run()
        :param cmd: request to process
        :return: tuple of three values:
            - XML response (or None for no output)
            - boolean to terminate the task or to keep it active
            - request to be subsequently processed after outputting the XML
                response in the first element (or Null to disable subsequent
                processing)
        """
        return self.run(cmd, length, frame)

    def stop(self, cmd, length=None, frame=None):
        """
        This method is executed when the task is interrupted by an error.
        If not overridden, it calls run()
        :param cmd: request to process
        :return: tuple of three values:
            - XML response (or None for no output)
            - boolean to terminate the task or to keep it active
            - request to be subsequently processed after outputting the XML
                response in the first element (or Null to disable subsequent
                processing)
        """
        return self.run(cmd, length, frame)

    def run(self, cmd, length=None, frame=None):
        """
        Main method to be overridden by the actual task; it is always run
            if start and stop are not overridden, otherwise it is run for the
            subsequent frames after the first one
        :param cmd: request to process
        :return: tuple of three values:
            - XML response (or None for no output)
            - boolean to terminate the task or to keep it active
            - request to be subsequently processed after outputting the XML
                response in the first element (or Null to disable subsequent
                processing)
        """
        return Tasks.TASK.PASSTHROUGH(cmd)


class Multiline(Tasks):
    """
    Special task to aggregate a multiline request into a single string
    before processing the request.
    """

    def run(self, cmd, length=None, frame=None):
        """
        Compose a multiline request. Call it on each request fragment,
        passing the standard method parameters, until data is returned.

        :param cmd: frame data (excluding header and length)
        :param length: decimal value of the length byte of a multiline frame
        :param frame: can be None (single frame), 0 (First Frame) or > 0 (subsequent frame)
        :return:
            error = Tasks.TASK.ERROR
            incomplete request = Tasks.TASK.INCOMPLETE
            complete request = Tasks.TASK.PASSTHROUGH(cmd)
        """
        if frame is not None and frame == 0 and length > 0: # First Frame (FF)
            if self.frame or self.length:
                self.logging.error('Invalid initial frame %s %s', length, cmd)
                return Tasks.TASK.ERROR
            self.req = cmd
            self.frame = 1
            self.length = length
        elif (frame is not None and frame > 0 and self.frame == frame and
              length is None): # valid Consecutive Frame (CF)
            self.req += cmd
            self.frame += 1
        elif ((length is None or length > 0) and
              frame is None and self.frame is None): # Single Frame (SF)
            self.req = cmd
            self.length = length
            if length:
                return Tasks.TASK.PASSTHROUGH(self.req[:self.length * 2])
            else:
                return Tasks.TASK.PASSTHROUGH(self.req)
        else:
            self.logging.error('Invalid consecutive frame %s %s', frame, cmd)
            return Tasks.TASK.ERROR

        # Process Flow Control (FC)
        if self.flow_control:
            self.flow_control -= 1
        else:
            if ('cmd_cfc' not in self.emulator.counters or
                    self.emulator.counters['cmd_cfc'] == 1):
                resp = self.emulator.handle_response(
                    ('<flow>' + hex(self.flow_control_end)[2:].upper() +
                     ' 00</flow>'),
                    do_write=self.do_write,
                    request_header=self.header,
                    request_data=cmd)
                if not self.do_write:
                    self.logging.warning("Output data: %s", repr(resp))
            self.flow_control = self.flow_control_end - 1

        if self.length * 2 <= len(self.req):
            self.frame = None
            return Tasks.TASK.PASSTHROUGH(self.req[:self.length * 2])
        return Tasks.TASK.INCOMPLETE


class Elm:
    """
    Main class of the ELM327-emulator
    """
    ELM_VALID_CHARS = r"[a-zA-Z0-9 \n\r]*"

    class THREAD:
        """
        Possible states for the Context Manager thread
        """
        STOPPED = 0
        STARTING = 1
        ACTIVE = 2
        PAUSED = 3
        TERMINATED = 4

    def Sequence(self, pid, base, max, factor, n_bytes):
        c = self.counters[pid]
        # compute the new value [= factor * ( counter % (max * 2) )]
        p = int(factor * abs(max - (c + max) % (max * 2))) + base
        # get its hex string
        s = ("%.X" % p).zfill(n_bytes * 2)
        # space the string into chunks of two bytes
        return (" ".join(s[i:i + 2] for i in range(0, len(s), 2)))

    def reset(self, sleep):
        """ returns all settings to their defaults.
            Called by __init__(), ATZ and ATD.
        """
        logging.debug("Resetting counters and sleeping for %s seconds", sleep)
        time.sleep(sleep)
        for i in [k for k in self.counters if k.startswith('cmd_')]:
            del (self.counters[i])
        self.counters['ELM_PIDS_A'] = 0
        self.counters['ELM_MIDS_A'] = 0
        self.counters['cmd_set_header'] = ECU_ADDR_E
        self.counters.update(self.presets)
        self.tasks = {}

    def set_defaults(self):
        """
        Called by __init__() and terminate()
        """
        self.scenario = 'default'
        self.delay = 0
        self.max_req_timeout = 1440
        self.answer = {}
        self.counters = {}
        self.counters.update(self.presets)

    def setSortedOBDMsg(self, scenario=None):
        if scenario is not None:
            self.scenario = scenario
        if 'default' in self.ObdMessage and 'AT' in self.ObdMessage:
            # Perform a union of the three subdictionaries
            self.sortedOBDMsg = {
                **self.ObdMessage['default'],  # highest priority
                **self.ObdMessage['AT'],
                **self.ObdMessage[self.scenario]  # lowest priority ('Priority' to be checked)
            }
        else:
            self.sortedOBDMsg = {**self.ObdMessage[self.scenario]}
        # Add 'Priority' to all pids and sort basing on priority (highest = 1, lowest=10)
        self.sortedOBDMsg = sorted(
            self.sortedOBDMsg.items(),
            key=lambda x: x[1]['Priority'] if 'Priority' in x[1] else 10)

    def __init__(
            self,
            batch_mode=False,
            newline=False,
            serial_port=None,
            device_port=None,
            serial_baudrate="",
            net_port=None,
            forward_net_host=None,
            forward_net_port=None,
            forward_serial_port=None,
            forward_serial_baudrate = None,
            forward_timeout=None):
        self.presets = {}
        self.ObdMessage = ObdMessage
        self.ELM_R_UNKNOWN = ELM_R_UNKNOWN
        self.set_defaults()
        self.setSortedOBDMsg()
        self.batch_mode = batch_mode
        self.newline = newline
        self.serial_port = serial_port
        self.device_port = device_port
        self.serial_baudrate = serial_baudrate
        self.net_port = net_port
        self.forward_net_host = forward_net_host
        self.forward_net_port = forward_net_port
        self.forward_serial_port = forward_serial_port
        self.forward_serial_baudrate = forward_serial_baudrate
        self.forward_timeout = forward_timeout
        self.reset(0)
        self.slave_name = None # pty port name, if pty is used
        self.master_fd = None # pty port FD, if pty is used, or device com port FD (IO)
        self.slave_fd = None # pty side used by the client application
        self.serial_fd = None # serial COM port file descriptor (pySerial)
        self.sock_inet = None
        self.fw_sock_inet = None
        self.fw_serial_fd = None
        self.sock_conn = None
        self.sock_addr = None
        self.thread = None
        self.aaa = device_port
        self.plugins = {}

    def __enter__(self):
        # start the read thread
        self.threadState = self.THREAD.STARTING
        self.thread = threading.Thread(target=self.run)
        self.thread.daemon = True
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.terminate()
        return False  # don't suppress any exceptions

    def terminate(self):
        """ termination procedure """
        logging.debug("Start termination procedure.")
        if (self.thread and
                self.threadState != self.THREAD.STOPPED and
                self.threadState != self.THREAD.TERMINATED):
            time.sleep(0.1)
            try:
                self.thread.join(1)
            except:
                logging.debug("Cannot join current thread.")
            self.thread = None
        self.threadState = self.THREAD.TERMINATED
        try:
            if self.slave_fd:
                os.close(self.slave_fd)
            if self.master_fd: # pty or device
                thread = threading.Thread(
                    target=os.close, args=(self.master_fd,))
                thread.start()
                thread.join(1)
                if thread.is_alive():
                    logging.critical(
                        'Cannot close file descriptor. '
                        'Forcing program termination.')
                    os._exit(5)
            if self.serial_fd: # serial COM - pySerial
                self.reset_input_buffer()
                self.reset_output_buffer()
                self.serial_fd.close()
            if self.sock_inet:
                self.sock_inet.shutdown(socket.SHUT_RDWR)
                self.sock_inet.close()
        except:
            logging.debug("Cannot close file descriptors.")
        self.set_defaults()
        logging.debug("Terminated.")
        return True

    def socket_server(self):
        """
            create an INET, STREAMing socket
            set self.sock_inet
        """
        if self.sock_inet:
            self.sock_inet.shutdown(socket.SHUT_RDWR)
            self.sock_inet.close()
        self.sock_conn = None
        self.sock_addr = None
        errmsg = "Unknown error"
        HOST = "0.0.0.0"
        for res in socket.getaddrinfo(HOST, self.net_port,
                                      socket.AF_UNSPEC,
                                      socket.SOCK_STREAM, 0,
                                      socket.AI_PASSIVE):
            af, socktype, proto, canonname, sa = res
            try:
                self.sock_inet = socket.socket(af, socktype, proto)
                self.sock_inet.setsockopt(
                    socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.sock_inet.setsockopt(
                    socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError as msg:
                errmsg = msg
                self.sock_inet = None
                continue

            try:
                # Bind the socket to the port
                self.sock_inet.bind((NETWORK_INTERFACES, self.net_port))
                # Become a socket server and listen for incoming connections
                self.sock_inet.listen(1)
            except OSError as msg:
                errmsg = msg
                self.sock_inet.close()
                self.sock_inet = None
                continue

            break

        if self.sock_inet is None:
            logging.error(
                "Local socket %s creation failed: %s.",
                self.net_port, errmsg)
            return False

        return True

    def connect_serial(self):
        """
        Shall be called after get_pty() and before a read operation.
        It opens the serial port, if not yet opened.
        It is expected to be blocking.
        Returns True if the serial or pty port is opened,
        or None in case of error.
        """

        # if the port is already opened, return True...
        if self.slave_name or self.master_fd or self.serial_fd:
            return True

        # else open the port
        if self.device_port: # os IO
            try:
                self.master_fd = os.open(self.device_port, os.O_RDWR)
            except Exception as e:
                logging.critical("Error while opening device %s:\n%s",
                                 repr(self.device_port), e)
                return None
            return True
        elif self.serial_port: # pySerial COM
            try:
                self.serial_fd = serial.Serial(
                    port=self.serial_port,
                    baudrate=self.serial_baudrate or SERIAL_BAUDRATE)
                self.slave_name = self.get_port_name(extended=True)
            except Exception as e:
                logging.critical("Error while opening serial COM %s:\n%s",
                                 repr(self.serial_port), e)
                return None
            return True
        else:
            return False

    def get_pty(self):
        """
        Return the opened pty port, or None if the pty port
        cannot be opened (non UNIX system).
        In case of UNIX system and if the port is not yet opened, open it.
        It is not blocking.
        """

        # if the port is already opened, return the port name...
        if self.slave_name:
            return self.slave_name
        elif self.master_fd and self.device_port:
            return self.device_port
        elif self.serial_fd and self.serial_port:
            return self.serial_port
        elif self.master_fd:
            logging.critical("Internal error, no configured device port.")
            return None
        elif self.serial_fd:
            logging.critical("Internal error, no configured COM port.")
            return None

        # ...else, with a UNIX system, make a new pty
        self.slave_fd = None

        if os.name == 'nt':
            self.slave_fd = None
            return None
        else:
            if not self.device_port and not self.serial_port:
                self.master_fd, self.slave_fd = pty.openpty()
                self.slave_name = os.ttyname(self.slave_fd)
                logging.debug("Pty name: %s", self.slave_name)

        return self.slave_name

    def run(self):  # daemon thread
        """
        This is the core procedure.

        Can be run directly or by the Context Manager through
        the thread: ref. __enter__()

        No return code.
        """
        setup_logging()
        self.logger = logging.getLogger()
        if self.net_port:
            if not self.socket_server():
                logging.critical("Net connection failed.")
                self.terminate()
                return False
        else:
            if (not self.device_port and
                    not self.serial_port and
                    not self.get_pty()):
                if os.name == 'nt':
                    logging.critical("Invalid setting for Windows.")
                else:
                    logging.critical("Pseudo-tty port connection failed.")
                self.terminate()
                return False
        self.choice = choice

        if self.sock_inet:
            if self.net_port:
                msg = 'at ' + self.get_port_name()
            else:
                msg = 'with no open TCP/IP port.'
        else:
            msg = 'on ' + self.get_port_name()
        if self.batch_mode:
            logging.debug(
                'ELM327 OBD-II adapter emulator v%s started '
                '%s_______________', __version__, msg)
        else:
            logging.info(
                '\n\nELM327 OBD-II adapter emulator v%s started '
                '%s\n', __version__, msg)
        """ the ELM's main IO loop """

        # Load and validate plugins
        self.plugins = {
            name: importlib.import_module(PLUGIN_DIR + "." + name)
            for finder, name, ispkg
            in pkgutil.iter_modules(
                importlib.import_module(PLUGIN_DIR).__path__)
            if name.startswith('task_')
        }
        remove = []
        for k, v in self.plugins.items():
            if (not (hasattr(v, "Task")) or
                    not inspect.isclass(v.Task)):
                logging.critical(
                    "Task class not available in plugin %s", k)
                remove += [k]
                continue
        for k in remove:
            del self.plugins[k]

        self.threadState = self.THREAD.ACTIVE
        while (self.threadState != self.THREAD.STOPPED and
               self.threadState != self.THREAD.TERMINATED):
            if self.threadState == self.THREAD.PAUSED:
                time.sleep(0.1)
                continue

            # get the latest command
            self.cmd = self.normalized_read_line()
            if (self.threadState == self.THREAD.STOPPED or
                    self.threadState == self.THREAD.TERMINATED):
                return True
            if self.cmd is None:
                continue

            # process 'fast' option (command repetition)
            if re.match('^ *$', self.cmd) and "cmd_last_cmd" in self.counters:
                self.cmd = self.counters["cmd_last_cmd"]
                logging.debug("repeating previous command: %s", repr(self.cmd))
            else:
                self.counters["cmd_last_cmd"] = self.cmd
                logging.debug("Received %s", repr(self.cmd))

            # if it didn't contain any egregious errors, handle it
            if self.validate(self.cmd):
                try:
                    request_header, request_data, resp = self.handle_request(
                        self.cmd, do_write=True)
                except Exception as e:
                    logging.critical("Error while processing %s:\n%s\n%s",
                                     repr(self.cmd), e, traceback.format_exc())
                    continue
                if resp is not None:
                    resp = self.handle_response(
                        resp,
                        do_write=True,
                        request_header=request_header,
                        request_data=request_data)
            else:
                logging.warning("Invalid request: %s", repr(self.cmd))
        return True

    def accept_connection(self):
        if self.sock_conn is None or self.sock_addr is None:

            # Accept network connections
            try:
                logging.debug(
                    "Waiting for connection at %s", self.get_port_name())
                (self.sock_conn, self.sock_addr) = self.sock_inet.accept()
            except OSError as msg:
                if msg.errno == errno.EINVAL: # [Errno 22] invalid argument
                    return False
                logging.error("Failed accepting connection: %s", msg)
                return False
            logging.debug("Connected by %s", self.sock_addr)
        return True

    def serial_client(self):
        if self.fw_serial_fd:
            return True
        try:
            self.fw_serial_fd = serial.Serial(
                port=self.forward_serial_port,
                baudrate=int(self.forward_serial_baudrate)
                    if self.forward_serial_baudrate else SERIAL_BAUDRATE,
                timeout=self.forward_timeout or FORWARD_READ_TIMEOUT)
            return True
        except Exception as e:
            logging.error('Cannot open forward port: %s', e)
            return False

    def net_client(self):
        if (self.fw_sock_inet
                or self.forward_net_host is None
                or self.forward_net_port is None):
            return
        s = None
        for res in socket.getaddrinfo(
                self.forward_net_host, self.forward_net_port,
                socket.AF_UNSPEC, socket.SOCK_STREAM):
            af, socktype, proto, canonname, sa = res
            try:
                s = socket.socket(af, socktype, proto)
                self.sock_inet.setsockopt(
                    socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.sock_inet.setsockopt(
                    socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError as msg:
                s = None
                continue
            try:
                s.connect(sa)
                s.settimeout(self.forward_timeout or FORWARD_READ_TIMEOUT)
            except OSError as msg:
                s.close()
                s = None
                continue
            break
        if s is None:
            logging.critical(
                "Cannot connect to host %s with port %s",
                self.forward_net_host, self.forward_net_port)
            self.terminate()
            return False
        self.fw_sock_inet = s
        return True

    def send_receive_forward(self, i):
        """
            If a forwarder is active, send data if it is not None
            and try receiving data until a timeout.
            Then received data are logged and returned.

            return False: no connection
            return None: no data
            return data: decoded string
        """

        if self.forward_serial_port:
            if self.fw_serial_fd is None:
                if not self.serial_client():
                    return False
            if self.fw_serial_fd:
                if i:
                    self.fw_serial_fd.write(i)
                    logging.info(
                        "Write forward data: %s", repr(i))
                proxy_data = self.fw_serial_fd.read(1024)
                logging.info(
                    "Read forward data: %s", repr(proxy_data))
                return repr(proxy_data)
            return False

        if not self.forward_net_host or not self.forward_net_port:
            return False
        if self.fw_sock_inet is None:
            self.net_client()
        if self.fw_sock_inet:
            if i:
                try:
                    self.fw_sock_inet.sendall(i)
                    logging.info(
                        "Write forward data: %s", repr(i))
                except BrokenPipeError:
                    logging.error(
                        "The network link of the OBDII interface dropped.")
            try:
                proxy_data = self.fw_sock_inet.recv(1024)
                logging.info(
                    "Read forward data: %s", repr(proxy_data))
                return proxy_data.decode("utf-8", "ignore")
            except socket.timeout:
                logging.info(
                    "No forward data received.")
                return None
        return False

    def get_port_name(self, extended=False):
        """
        Returns the name of the opened port.
        :param extended: False or True
        :return: string
        """
        if self.sock_inet:
            if self.net_port:
                postfix = ''
                if extended:
                    postfix = '\nWarning: the socket is bound '\
                              'to all interfaces.'
                return ('TCP/IP network port ' + str(self.net_port) + '.'
                        + postfix)
            else:
                return ('Unopened TCP/IP network port ' +
                        str(self.net_port) + '.')

        if self.device_port:
            if os.name == 'nt':
                return ('(invalid) OS communication device "' +
                        self.device_port + '".')
            else:
                return ('OS communication device "' +
                        self.device_port + '".')

        if self.serial_port:
            postfix = ''
            baudrate = ''
            if extended:
                postfix = ' of com0com COM port pair'
                if self.serial_baudrate:
                    baudrate = " with baud rate " + str(
                        self.serial_baudrate or SERIAL_BAUDRATE)

            if os.name == 'nt':
                if self.serial_port == 'COM3':
                    return ('Windows serial COM port "' + self.serial_port + '"'
                            + postfix + baudrate + '.')
                else:
                    return ('serial COM port "' + self.serial_port + '"'
                            + baudrate + '.')
            else:
                return 'serial communication port "' + self.serial_port + '".'

        if self.slave_name:
            if os.name == 'nt':
                return "(invalid) Windows PTY " + self.slave_name + '.'
            else:
                return ('pseudo-tty port "' +
                        self.slave_name + '".')

        return 'unknown port.'

    def read_from_device(self, bytes):
        """
            read from the port; returns up to bytes characters
            (generally 1)
            and processes echo; returns None in case of error
        """

        # Process inet
        c = None
        if self.sock_inet:
            if not self.accept_connection():
                return None
            try:
                c = self.sock_conn.recv(bytes)
                if len(c) == 0:
                    logging.debug(
                        "TCP/IP communication terminated by the client.")
                    self.sock_conn = None
                    self.sock_addr = None
                    self.reset(0)
                    return None
            except ConnectionResetError:
                logging.warning(
                    "Session terminated by the client.")
                self.sock_conn = None
                self.sock_addr = None
                self.reset(0)
                return None
            except UnicodeDecodeError as msg:
                logging.error(
                    "UTF8 decode error: %s", msg)
                return None
            except Exception as msg:
                logging.error(
                    "Error while reading from network: %s", msg)
                return None
            if 'cmd_echo' not in self.counters or (
                    'cmd_echo' in self.counters and
                    self.counters['cmd_echo']):
                self.sock_conn.sendall(c)
            return c

        # Process serial (COM or device)
        try:
            if not self.connect_serial():
                self.terminate()
                return None

            # Serial COM port (uses pySerial)
            if self.serial_fd and self.serial_port:
                try:
                    c = self.serial_fd.read(bytes)
                except Exception:
                    logging.debug(
                        'Error while reading from %s', self.get_port_name())
                    return None
                if 'cmd_echo' not in self.counters or (
                        'cmd_echo' in self.counters and
                        self.counters['cmd_echo']):
                    self.serial_fd.write(c)

            # Device port (use os IO)
            else:
                if not self.master_fd:
                    logging.critical(
                        "PANIC - Internal error, missing device FD")
                    self.terminate()
                    return None
                c = os.read(self.master_fd, bytes)
                if 'cmd_echo' not in self.counters or (
                        'cmd_echo' in self.counters and
                        self.counters['cmd_echo']):
                    try:
                        os.write(self.master_fd, c)
                    except OSError as e:
                        if e.errno == errno.EBADF or e.errno == errno.EIO:  # [Errno 9] Bad file descriptor/[Errno 5] Input/output error
                            logging.debug("Read interrupted. Terminating.")
                            self.terminate()
                            return None
                        else:
                            logging.critical(
                                "PANIC - Internal OSError in read(): %s",
                                e, exc_info=True)
                            self.terminate()
                            return None
        except UnicodeDecodeError as e:
            logging.warning("Invalid character received: %s", e)
            return None
        except OSError:
            return None
        return c

    def normalized_read_line(self):
        """
            reads the next newline delimited command
            returns a normalized string command
        """
        buffer = ""
        first = True

        req_timeout = self.max_req_timeout
        try:
            req_timeout = float(self.counters['req_timeout'])
        except Exception as e:
            if 'req_timeout' in self.counters:
                logging.error("Improper configuration of\n\"self.counters" \
                              "['req_timeout']\": '%s' (%s). "
                              "Resetting it to %s",
                              self.counters['req_timeout'], e,
                              self.max_req_timeout
                              )
            self.counters['req_timeout'] = req_timeout
        while True:
            prev_time = time.time()
            c = self.read_from_device(1)
            if c is None:
                return None
            c = c.decode("utf-8", "ignore")
            if prev_time + req_timeout < time.time() and first == False:
                buffer = ""
                logging.debug("'req_timeout' timeout while reading data: %s", c)
            if c == '\r':
                if self.newline:
                    continue
                break
            if c == '\n':
                if self.newline:
                    break
                continue  # ignore newlines
            first = False
            buffer += c

        try:
            self.send_receive_forward((buffer + '\r').encode())
        except Exception as e:
            logging.error('Forward Write error: %s', e)
        return buffer

    def write_to_device(self, i):
        """
        write a response to the port (no data returned).
        No return code.
        """

        # Process inet
        if self.sock_inet:
            if not self.accept_connection():
                self.terminate()
                return
            try:
                self.sock_conn.sendall(i)
            except BrokenPipeError:
                logging.error("Connection dropped.")
            return

        # Process serial
        if self.serial_fd:

            # Serial COM port (uses pySerial)
            try:
                self.serial_fd.write(i)
            except Exception:
                logging.debug(
                    'Error while writing to %s', self.get_port_name())
            return

        else:
            # Device port (use os IO)
            if not self.master_fd:
                logging.critical(
                    "PANIC - Internal error, missing device FD")
                self.terminate()
                return
            try:
                os.write(self.master_fd, i)
            except OSError as e:
                if e.errno == errno.EBADF or e.errno == errno.EIO:  # [Errno 9] Bad file descriptor/[Errno 5] Input/output error
                    logging.debug("Read interrupted. Terminating.")
                    self.terminate()
                    return
                else:
                    logging.critical(
                        "PANIC - Internal OSError in write(): %s", e,
                        exc_info=True)
                    self.terminate()
                    return

    def uds_answer(
            self, data, request_header, use_headers, sp, flow_control=None):
        answer = ""
        if request_header is None and "cmd_set_header" in self.counters:
            request_header = self.counters['cmd_set_header']
        request_header = request_header.translate(
            request_header.maketrans('', '', string.whitespace)).upper()
        if not request_header:
            logging.error('Invalid request header; request %s', repr(data))
            return ""
        try:
            length = len(bytearray.fromhex(data))
            data = (sp.join('{:02x}'.format(x)
                             for x in bytearray.fromhex(data)).upper())
        except ValueError:
            logging.error('Invalid data in answer: %s', repr(data))
            return ""
        if len(request_header) == 3 and flow_control:
            if use_headers:
                answer = hex(int(request_header, 16) + 8)[2:].upper() + sp
            answer += flow_control + sp + data
        elif len(request_header) == 3:
            if use_headers:
                answer = (hex(int(request_header, 16) + 8)[2:].upper() + sp +
                          "%02X"%length + sp)
            answer += data
        elif len(request_header) == 6 and flow_control:
            logging.error('Unimplemented flow control.')
            return ""
        elif len(request_header) == 6:
            if not use_headers:
                logging.error('Unimplemented case.')
                return ""
            if length < 10:
                answer = ((hex(128 + length)[2:].upper() + sp +
                           request_header[4:6] + sp +
                           request_header[2:4]) + sp + data)
            else:
                answer = ("80 " + # Extra length byte follows
                          request_header[4:6] + sp +
                          request_header[2:4] + sp +
                          hex(length)[2:].upper() + sp + data)
            answer += " %02X" % (sum(bytearray.fromhex(answer)) % 256)
        else:
            logging.error('Invalid request header: %s', repr(request_header))
        return answer

    def handle_response(self,
                        resp,
                        do_write=False,
                        request_header=None,
                        request_data=None):
        """ compute the response and returns data written to the device """

        logging.debug("Processing: %s", repr(resp))

        # compute cra_pattern
        cra_pattern = r'.*'
        cra = self.counters["cmd_cra"] if "cmd_cra" in self.counters else None
        if cra:
            cra = cra.replace('X', '.').replace('x', '?')
            cra_pattern = r'^' + cra + r'$'

        # compute use_headers
        use_headers = ("cmd_use_header" in self.counters and
                self.counters["cmd_use_header"])

        # compute sp
        if ('cmd_spaces' in self.counters
                and self.counters['cmd_spaces'] == 0):
            sp = ''
        else:
            sp = ' '

        # compute nl
        nl_type = {
            0: "\r",
            1: "\r\n",
            2: "\n",
            3: "\r",
            4: "\r\n",
            5: "\n"
        }
        nl = "\r"
        if 'cmd_linefeeds' in self.counters:
            try:
                nl = nl_type[int(self.counters['cmd_linefeeds'])]
            except Exception:
                logging.error(
                    'Invalid "cmd_linefeeds" value: %s.',
                    repr(self.counters['cmd_linefeeds']))

        # generate string
        incomplete_resp = False
        root = None
        try:
            root = ET.fromstring('<xml>' + resp + '</xml>')
            s = iter(root)
        except ET.ParseError as e:
            incomplete_resp = True
            logging.error(
                'Wrong response format for "%s"; %s', resp, e)
        answ = root.text.strip() if root is not None and root.text else ""
        answers = False
        i = None
        while not incomplete_resp:
            try:
                i = next(s)
            except StopIteration:
                answ += i.tail.strip() if i is not None and i.tail else ""
                break
            if i.tag.lower() == 'rh':
                request_header = (i.text or "")
            elif i.tag.lower() == 'rd':
                request_data = (i.text or "")
            elif i.tag.lower() == 'string':
                answ += (i.text or "")
            elif i.tag.lower() == 'writeln':
                answ += (i.text or "") + nl
            elif i.tag.lower() == 'space':
                answ += (i.text or "") + sp
            elif (i.tag.lower() == 'eval' or
                    i.tag.lower() == 'exec'):
                logging.debug("Write: %s", repr(answ))
                if i.tag.lower() == 'exec' and do_write:
                    self.write_to_device(answ.encode())
                    answ = ""
                if i.text is None:
                    continue
                msg = i.text.strip()
                if msg:
                    try:
                        evalmsg = eval(msg)
                        logging.debug(
                            "Evaluated command: %s -> %s",
                            msg, repr(evalmsg))
                        if evalmsg != None:
                            answ += str(evalmsg)
                    except Exception:
                        try:
                            exec(msg, globals())
                            logging.debug("Executed command: %s", msg)
                        except Exception as e:
                            logging.error("Cannot execute '%s': %s", msg, e)
                else:
                    logging.debug(
                        "Missing command to execute: %s", resp)

            elif i.tag.lower() == 'flow':
                answ += self.uds_answer(data=i.text or "",
                                        request_header=request_header,
                                        use_headers=use_headers,
                                        sp=sp,
                                        flow_control='30') + sp + nl

            elif i.tag.lower() == 'answer':
                answ += self.uds_answer(data=i.text or "",
                                        request_header=request_header,
                                        use_headers=use_headers,
                                        sp=sp) + sp + nl
            elif i.tag.lower() == 'pos_answer':
                if not request_data:
                    logging.error(
                        'Missing request with <pos_answer> tag: %s.',
                        repr(resp))
                    break
                try:
                    request_data = (''.join('{:02x}'.format(x)
                                     for x in bytearray.fromhex(
                                        request_data)).upper())
                except ValueError:
                    logging.error('Invalid request %s related to response %s '
                                  'including <pos_answer> tag.',
                                  repr(request_data), repr(resp))
                    return ""
                data = ("%02X"%(bytearray.fromhex(request_data[:2])[0] + 64) +
                        request_data[2:4] + (i.text or ""))
                answ += self.uds_answer(data=data,
                                        request_header=request_header,
                                        use_headers=use_headers,
                                        sp=sp) + sp + nl

            elif i.tag.lower() == 'neg_answer':
                if not request_data:
                    logging.error(
                        'Missing request with <neg_answer> tag: %s.',
                        repr(resp))
                    break
                try:
                    request_data = (''.join('{:02x}'.format(x)
                                     for x in bytearray.fromhex(
                                        request_data)).upper())
                except ValueError:
                    logging.error('Invalid request %s related to response %s '
                                  'including <neg_answer> tag.',
                                  repr(request_data), repr(resp))
                    return ""
                data = "7F " + request_data[:2] + (i.text or "")
                answ += self.uds_answer(data=data,
                                        request_header=request_header,
                                        use_headers=use_headers,
                                        sp=sp) + sp + nl

            elif i.tag.lower() == 'header':
                answers = True
                incomplete_resp = True
                try:
                    size = next(s)
                    data = next(s)
                except StopIteration:
                    logging.error(
                        'Missing <size> or <data>/<subd> tags '
                        'after <header> tag in %s.', repr(resp))
                    break
                # check that the tags are valid
                if (size.tag.lower() != 'size' or
                        (data.tag.lower() != 'data' and
                         data.tag.lower() != 'subd')):
                    logging.error(
                        'In %s, <size> and <data>/<subd> tags '
                        'must follow the <header> tag.', repr(resp))
                    break

                # check validity of the content fields
                try:
                    int_size = int(size.text, 16)
                except ValueError as e:
                    logging.error(
                        'Improper size %s for response %s: %s.',
                        repr(size.text), repr(resp), e)
                    break
                if not data.text:
                    logging.error('Missing data for response %s.',
                                  repr(resp))
                    break
                unspaced_data = (data.text or "").translate(
                    answ.maketrans('', '', string.whitespace))
                if int_size < 16 and len(unspaced_data) != int_size * 2:
                    logging.error(
                        'In response %s, mismatch between number of data '
                        'digits %s and related length field %s.',
                        repr(resp), repr(data.text), repr(size.text))
                    break
                incomplete_resp = False
                if re.match(cra_pattern, i.text):
                    # concatenate answ from header, size and data/subd
                    answ += ((((i.text or "") + sp + (size.text or "") + sp)
                            if use_headers else "") +
                            ((data.text or "") if sp else unspaced_data) +
                            sp + (nl if data.tag.lower() == 'data' else ""))
            else:
                logging.error(
                    'Unknown tag "%s" in response "%s"', i.tag, resp)
            answ += i.tail.strip() if i is not None and i.tail else ""
        if incomplete_resp or (answers and not answ):
            answ = "NO DATA" + nl
        if not answ:
            logging.debug(
                'Null response received after processing "%s".', resp)
            return None
        if ('cmd_linefeeds' in self.counters and
                self.counters['cmd_linefeeds'] > 2):
            answ += ">"
        else:
            answ += nl + ">"
        if do_write:
            logging.debug("Write: %s", repr(answ))
            self.write_to_device(answ.encode())
        return answ

    def validate(self, cmd):
        if not re.match(self.ELM_VALID_CHARS, cmd):
            return False
        return True

    def task_action(self, header, do_write, task_method, cmd, length, frame):
        """
        Call a task method (start(), run(), or stop()), manage the exception,
         pre-process r_task and r_cont return values and return a tuple with
         all the three return values of the invoked method.

        :param header:
        :param do_write:
        :param task_method:
        :param length:
        :param frame:
        :param cmd:
        :return: a tuple of three elements with the same return parameters
                as the Task methods
        """
        logging.debug(
            "Running task %s.%s(%s, %s, %s) with header %s",
            self.tasks[header].__module__, task_method.__name__, cmd, length,
            frame, header)

        r_cmd = None
        r_task = Tasks.TASK.TERMINATE
        r_cont = None
        try:
            r_cmd, r_task, r_cont = task_method(cmd, length, frame)
        except Exception as e:
            logging.critical(
                'Error in task "%s", header="%s", '
                'method=%s(): %s',
                self.tasks[header].__module__,
                header,
                task_method.__name__,
                e, exc_info=True)
            del self.tasks[header]
            return Tasks.TASK.ERROR
        logging.debug("r_cmd=%s, r_task=%s, r_cont=%s", r_cmd, r_task, r_cont)
        if r_cont is not None:
            if r_cmd is not None:
                resp = self.handle_response(
                    r_cmd,
                    do_write=do_write,
                    request_header=header,
                    request_data=self.tasks[header].task_get_request())
                if not do_write:
                    logging.warning("Task with header %s returned %s",
                                    header, repr(resp))
        if r_task is Tasks.TASK.TERMINATE:
            logging.debug(
                'Terminated task "%s" with header "%s"',
                self.tasks[header].__module__, header)
            self.account_task(header)
            del self.tasks[header]
        if r_cont is not None:
            logging.debug(
                "Continue processing command %s after execution of task "
                "with header %s.", repr(r_cont), header)
        return (r_cmd, r_task, r_cont)

    def account_task(self, header):
        if header not in self.tasks:
            return
        try:
            task_name = self.tasks[header].__module__[12:]
        except Exception:
            return
        if task_name not in self.counters:
            self.counters[task_name] = 0
        self.counters[task_name] += 1

    def handle_request(self, cmd, do_write=False):
        """
        It generates an XML response by processing a request,
        returning a string to be processed by process_response(),
        which creates the final output format. It returns None
        if this there are no data to be processed.

        In some cases, process_response() is implicitly called,
        passing do_write.

        :param cmd: the request to be processed
        :param do_write: passed to process_response() when
                        implicitly called, or used for logging.
        :return: (header, request, None or an XML string)
        """

        # Sanitize cmd
        cmd = cmd.replace(" ", "")
        cmd = cmd.upper()

        # increment 'commands' counter
        if 'commands' not in self.counters:
            self.counters['commands'] = 0
        self.counters['commands'] += 1

        header = None
        if "cmd_set_header" in self.counters:
            header = self.counters['cmd_set_header']

        # manages delay
        logging.debug("Handling: %s", repr(cmd))
        if self.delay > 0:
            time.sleep(self.delay)

        if not self.scenario in self.ObdMessage:
            logging.error("Unknown scenario %s", repr(self.scenario))
            return header, cmd, ""

        # cmd_fcsm is experimental (to be removed)
        if ('cmd_fcsm' in self.counters and
                self.counters['cmd_fcsm'] and
                cmd[:2] != 'AT'
                and self.is_hex_sp(cmd [:3])):
            self.counters['cmd_set_header'] = cmd[:3]
            header = cmd[:3]
            self.counters['cmd_caf'] = False
            self.counters['cmd_use_header'] = True
            cmd = cmd[3:]

        # manages cmd_caf, length, frame
        size = cmd[:2]
        length = None # no byte length in request
        frame = None # Single Frame
        if ('cmd_caf' in self.counters and
                not self.counters['cmd_caf'] and # PCI byte in requests
                size != 'AT' and self.is_hex_sp(size)):
            try:
                int_size = int(size, 16)
            except ValueError as e:
                logging.error('Improper size %s for request %s: %s',
                              repr(size), repr(cmd), e)
                return header, cmd, ""
            payload = cmd[2:]
            if not payload:
                logging.error('Missing data for request "%s"', repr(cmd))
                return header, cmd, ""
            if int_size < 16:
                if len(payload) < int_size * 2:
                    logging.error(
                        'In request %s, data %s has an improper length '
                        'of %s bytes', repr(cmd), repr(payload), size)
                    return header, cmd, ""
                cmd = payload[:int_size * 2]
                length = int_size
            elif int_size == 16:
                try:
                    length = int(cmd[2:4], 16)
                except ValueError as e:
                    logging.error('Improper size %s for request %s: %s',
                                  repr(cmd[2:4]), repr(cmd), e)
                    return header, cmd, ""
                cmd = cmd[4:]
                frame = 0
            else:
                cmd = cmd[2:]
            if int_size > 32:
                frame = int_size - 32
            logging.debug(
                "Length: %s, frame: %s, header: %s, cmd: %s",
                length, frame, header, cmd)

        # Manage active tasks
        if (header not in self.tasks and
                length is not None and frame is not None):
            self.tasks[header] = Multiline(self, header, cmd, None, do_write)
        if header in self.tasks:
            if self.len_hex(cmd):
                r_cmd, *_, r_cont = self.task_action(header, do_write,
                    self.tasks[header].run, cmd, length, frame)
                if r_cont is None:
                    return header, cmd, r_cmd
                else:
                    cmd = r_cont
                    frame = None
                    length = None
            else:
                logging.warning('Interrupted task "%s" with header "%s"',
                                self.tasks[header].__module__, header)
                r_cmd, *_, r_cont = self.task_action(header, do_write,
                    self.tasks[header].stop, cmd, length, frame)
                if header in self.tasks:
                    del self.tasks[header]
                if r_cont is None:
                    return header, cmd, r_cmd
                else:
                    cmd = r_cont

        # Process response for data stored in cmd
        for i in self.sortedOBDMsg:
            key = i[0]
            val = i[1]
            if 'Request' in val and re.match(val['Request'], cmd):
                if ('Header' in val and header and
                        val['Header'] != self.counters["cmd_set_header"]):
                    continue
                if key:
                    pid = key
                else:
                    pid = 'UNKNOWN'
                if pid not in self.counters:
                    self.counters[pid] = 0
                self.counters[pid] += 1
                if 'Action' in val and val['Action'] == 'skip':
                    logging.info("Received %s. PID %s. Action=%s", cmd, pid,
                                 val['Action'])
                    continue
                if 'Descr' in val:
                    logging.debug("Description: %s, PID %s (%s)",
                                  val['Descr'], pid, cmd)
                else:
                    logging.error(
                        "Internal error - Missing description for %s, PID %s",
                        cmd, pid)
                if pid in self.answer:
                    try:
                        return header, cmd, self.answer[pid]
                    except Exception as e:
                        logging.error(
                            "Error while processing '%s' for PID %s (%s)",
                            self.answer, pid, e)
                if 'Task' in val:
                    if val['Task'] in self.plugins:
                        if header in self.tasks:
                            logging.debug(
                                'Another task %s was active on the same header'
                                ' %s. Deleting it.',
                                self.tasks[header].__module__, header)
                            del self.tasks[header]
                        try:
                            self.tasks[header] = self.plugins[val['Task']].Task(
                                self, header, cmd, val, do_write)
                        except Exception as e:
                            logging.critical(
                                'Cannot start task "%s", header="%s": %s',
                                val['Task'], header, e, exc_info=True)
                            return header, cmd, None
                        logging.debug('Starting task "%s" with header "%s"',
                                      self.tasks[header].__module__, header)
                        r_cmd, *_, r_cont = self.task_action(header, do_write,
                            self.tasks[header].start, cmd, length, frame)
                        if r_cont is None:
                            return header, cmd, r_cmd
                        else:
                            cmd = r_cont
                    else:
                        logging.error(
                            'Unexisting plugin "%s" for pid "%s"',
                            val['Task'], pid)
                        return header, cmd, None
                if 'Exec' in val:
                    try:
                        exec(val['Exec'])
                    except Exception as e:
                        logging.error(
                            "Cannot execute '%s' for PID %s (%s)",
                            val['Exec'], pid, e)
                log_string = ""
                if 'Info' in val:
                    log_string = "logging.info(%s)"%val['Info']
                if 'Warning' in val:
                    log_string = "logging.warning(%s)"%val['Warning']
                if 'Log' in val:
                    log_string = "logging.debug(%s)"%val['Log']
                if log_string:
                    try:
                        exec(log_string)
                    except Exception as e:
                        logging.error(
                            "Error while logging '%s' for PID %s (%s)",
                            val['Log'], pid, e)
                if 'Response' in val:
                    r_header = ''
                    if 'ResponseHeader' in val:
                        r_header = val['ResponseHeader'](
                            self, cmd, pid, val)
                    r_footer = ''
                    if 'ResponseFooter' in val:
                        r_footer = val['ResponseFooter'](
                            self, cmd, pid, val)
                    r_response = val['Response']
                    if r_response is None:
                        return header, cmd, None
                    if isinstance(r_response, (list, tuple)):
                        r_response = r_response[randint(0, len(r_response) - 1)]
                    return header, cmd, (r_header + r_response + r_footer)
                else:
                    logging.error(
                        "Internal error - Missing response for %s, PID %s",
                        cmd, pid)
                    return header, cmd, ELM_R_OK
        # Here cmd is unknown
        if "unknown_" + repr(cmd) not in self.counters:
            self.counters["unknown_" + repr(cmd)] = 0
        self.counters["unknown_" + repr(cmd)] += 1
        if cmd == '':
            logging.info("No ELM command")
            return header, cmd, ""
        fw_data = self.send_receive_forward((cmd + '\r').encode())
        if fw_data is not False:
            self.counters["unknown_" + repr(cmd) + "_R"] = repr(fw_data)
        if (fw_data is not False and
                re.match(r"^NO DATA *\r", fw_data or "") is None and
                re.match(r"^\? *\r", fw_data or "") is None and
                self.counters["unknown_" + repr(cmd)] == 1):
            logging.warning(
                'Missing data in dictionary: %s. Answer:\n%s',
                repr(cmd), repr(fw_data))
        if self.len_hex(cmd):
            if header:
                logging.info("Unknown request: %s, header=%s",
                             repr(cmd), self.counters["cmd_set_header"])
            else:
                logging.info("Unknown request: %s", repr(cmd))
            return header, cmd, ST('NO DATA')
        if header:
            logging.info("Unknown ELM command: %s, header=%s",
                         repr(cmd), self.counters["cmd_set_header"])
        else:
            logging.info("Unknown ELM command: %s", repr(cmd))
        return header, cmd, self.ELM_R_UNKNOWN

    def is_hex_sp(self, s):
        return re.match(r"^[0-9a-fA-F \t\r\n]*$", s or "") is not None

    def len_hex(self, s):
        try:
            return len(bytearray.fromhex(s))
        except Exception:
            return False
