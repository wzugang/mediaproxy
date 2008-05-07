#
# Copyright (C) 2008 AG Projects
# Author: Ruud Klaver <ruud@ag-projects.com>
#

"""Implementation of the MediaProxy relay component"""


import random
import signal
import cjson

from twisted.protocols.basic import LineOnlyReceiver
from twisted.internet.protocol import Factory
from twisted.internet.defer import Deferred, DeferredList, maybeDeferred, succeed
from twisted.internet import epollreactor
epollreactor.install()
from twisted.internet import reactor

from gnutls.interfaces.twisted import X509Credentials

from application import log
from application.process import process
from application.configuration import *

from mediaproxy import configuration_filename, default_dispatcher_port
from mediaproxy.tls import Certificate, PrivateKey

class DispatcherAddress(datatypes.NetworkAddress):
    _defaultPort = default_dispatcher_port


class Config(ConfigSection):
    _datatypes = {"certificate": Certificate, "private_key": PrivateKey, "ca": Certificate, "listen": DispatcherAddress, "accounting": datatypes.StringList}
    socket = "/var/run/mediaproxy/dispatcher.sock"
    accounting = []
    certificate = None
    private_key = None
    ca = None
    listen = DispatcherAddress("any")
    relay_timeout = 5
    cleanup_timeout = 3600


configuration = ConfigFile(configuration_filename)
configuration.read_settings("Dispatcher", Config)

class OpenSERControlProtocol(LineOnlyReceiver):
    noisy = False

    def __init__(self):
        self.line_buf = []
        self.in_progress = 0

    def lineReceived(self, line):
        if line.strip() == "" and self.line_buf:
            self.in_progress += 1
            defer = self.factory.dispatcher.send_command(self.line_buf[0], self.line_buf[1:])
            defer.addCallback(self.reply)
            defer.addErrback(self._relay_error)
            defer.addErrback(self._catch_all)
            defer.addBoth(self._decrement)
            self.line_buf = []
        elif not line.endswith(": "):
            self.line_buf.append(line)

    def connectionLost(self, reason):
        log.debug("Connection to OpenSER lost: %s" % reason.value)
        self.factory.connection_lost(self)

    def reply(self, reply):
        self.transport.write(reply + "\r\n")

    def _relay_error(self, failure):
        failure.trap(RelayError)
        log.error("Error processing request: %s" % failure.value)
        self.transport.write("error\r\n")

    def _catch_all(self, failure):
        log.error(failure.getBriefTraceback())
        self.transport.write("error\r\n")

    def _decrement(self, result):
        self.in_progress = 0
        if self.factory.shutting_down:
            self.transport.loseConnection()


class OpenSERControlFactory(Factory):
    noisy = False
    protocol = OpenSERControlProtocol

    def __init__(self, dispatcher):
        self.dispatcher = dispatcher
        self.protocols = []
        self.shutting_down = False

    def buildProtocol(self, addr):
        prot = Factory.buildProtocol(self, addr)
        self.protocols.append(prot)
        return prot

    def connection_lost(self, prot):
        self.protocols.remove(prot)
        if self.shutting_down and len(self.protocols) == 0:
            self.defer.callback(None)

    def shutdown(self):
        if self.shutting_down:
            return
        self.shutting_down = True
        if len(self.protocols) == 0:
            return succeed(None)
        else:
            for prot in self.protocols:
                if prot.in_progress == 0:
                    prot.transport.loseConnection()
            self.defer = Deferred()
            return self.defer

class RelayError(Exception):
    pass


class RelayServerProtocol(LineOnlyReceiver):
    noisy = False

    def __init__(self):
        self.commands = {}
        self.ready = True
        self.sequence_number = 0
    
    def send_command(self, command, headers):
        log.debug('Issuing "%s" command to relay at %s' % (command, self.ip))
        seq = str(self.sequence_number)
        self.sequence_number += 1
        defer = Deferred()
        timer = reactor.callLater(Config.relay_timeout, self._timeout, seq, defer)
        self.commands[seq] = (command, defer, timer)
        self.transport.write("\r\n".join([" ".join([command, seq])] + headers + ["", ""]))
        return defer

    def _timeout(self, seq, defer):
        del self.commands[seq]
        defer.errback(RelayError("Relay at %s timed out" % self.ip))

    def lineReceived(self, line):
        try:
            first, rest = line.split(" ", 1)
        except ValueError:
            error.log("Could not decode reply from relay %s: %s" % (self.ip, line))
            return
        if first == "expired":
            try:
                stats = cjson.decode(rest)
            except cjson.DecodeError:
                log.error("Error decoding JSON from relay at %s" % self.ip)
            else:
                self.factory.dispatcher.update_statistics(stats)
                del self.factory.sessions[stats["call_id"]]
            return
        try:
            command, defer, timer = self.commands.pop(first)
        except KeyError:
            log.error("Got unexpected response from relay at %s: %s" % (self.ip, line))
            return
        timer.cancel()
        if rest == "error":
            defer.errback(RelayError('Received error from relay at %s in response to "%s" command' % (self.ip, command)))
        elif rest == "halting":
            self.ready = False
            defer.errback(RelayError("Relay at %s is shutting down" % self.ip))
        elif command == "remove":
            try:
                stats = cjson.decode(rest)
            except cjson.DecodeError:
                log.error("Error decoding JSON from relay at %s" % self.ip)
            else:
                self.factory.dispatcher.update_statistics(stats)
                del self.factory.sessions[stats["call_id"]]
            defer.callback("removed")
        else: # update command
            defer.callback(rest)

    def connectionLost(self, reason):
        log.debug("Relay at %s disconnected" % self.ip)
        for command, defer, timer in self.commands.itervalues():
            timer.cancel()
            defer.errback(RelayError("Relay at %s disconnected" % self.ip))
        self.factory.connection_lost(self.ip)


class RelayFactory(Factory):
    noisy = False
    protocol = RelayServerProtocol

    def __init__(self, dispatcher):
        self.dispatcher = dispatcher
        self.relays = {}
        self.sessions = {}
        self.cleanup_timers = {}
        self.shutting_down = False

    def buildProtocol(self, addr):
        ip = addr.host
        log.debug("Relay at %s connected" % ip)
        if ip in self.relays:
            log.error("Connection to relay %s is already present, disconnecting" % ip)
            return
        if ip in self.cleanup_timers:
            timer = self.cleanup_timers.pop(ip)
            timer.cancel()
        prot = Factory.buildProtocol(self, addr)
        prot.ip = ip
        self.relays[ip] = prot
        return prot

    def send_command(self, command, headers):
        call_id = None
        for header in headers:
            if header.startswith("call_id: "):
                call_id = header.split("call_id: ", 1)[1]
                break
        if call_id is None:
            raise RelayError("Could not parse call_id")
        if call_id in self.sessions:
            relay = self.sessions[call_id]
            if relay not in self.relays:
                raise RelayError("Relay for this session (%s) is no longer connected" % relay)
            return self.relays[relay].send_command(command, headers)
        elif command == "update":
            preferred_relay = None
            for header in headers:
                if header.startswith("media_relay: "):
                    preferred_relay = header.split("media_relay: ", 1)[1]
                    break
            if preferred_relay is not None:
                try_relays = [protocol for protocol in self.relays.itervalues() if protocol.ip == preferred_relay]
                other_relays = [protocol for protocol in self.relays.itervalues() if protocol.ready and protocol.ip != preferred_relay]
                random.shuffle(other_relays)
                try_relays.extend(other_relays)
            else:
                try_relays = [protocol for protocol in self.relays.itervalues() if protocol.ready]
                random.shuffle(try_relays)
            defer = self._try_next(try_relays, command, headers)
            defer.addCallback(self._add_session, try_relays, call_id)
            return defer
        else:
            raise RelayError("Non-update command received from OpenSER for unknown session")

    def _add_session(self, result, try_relays, call_id):
        self.sessions[call_id] = try_relays[-1].ip
        return result

    def _relay_error(self, failure, try_relays, command, headers):
        failure.trap(RelayError)
        failed_relay = try_relays.pop()
        log.warn("Relay from %s:%d returned error: %s" % (failed_relay.ip, failure.value))
        return self._try_next(try_relays, command, headers)

    def _try_next(self, try_relays, command, headers):
        if len(try_relays) == 0:
            raise RelayError("No suitable relay found")
        defer = try_relays[-1].send_command(command, headers)
        defer.addErrback(self._relay_error, try_relays, command, headers)
        return defer

    def connection_lost(self, ip):
        del self.relays[ip]
        if self.shutting_down:
            if len(self.relays) == 0:
                self.defer.callback(None)
        else:
            self.cleanup_timers[ip] = reactor.callLater(Config.cleanup_timeout, self._do_cleanup, ip)

    def _do_cleanup(self, ip):
        log.debug("Doing cleanup for old relay %s" % ip)
        del self.cleanup_timers[ip]
        for call_id in [call_id for call_id, relay in self.sessions.items() if relay == ip]:
            del self.sessions[call_id]

    def shutdown(self):
        if self.shutting_down:
            return
        self.shutting_down = True
        for timer in self.cleanup_timers.itervalues():
            timer.cancel()
        if len(self.relays) == 0:
            return succeed(None)
        else:
            for prot in self.relays.itervalues():
                prot.transport.loseConnection()
            self.defer = Deferred()
            return self.defer


class Dispatcher(object):

    def __init__(self):
        for value in [Config.certificate, Config.private_key, Config.ca]:
            if value is None:
                raise ValueError("TLS certificate/key pair and CA have not been set.")
        self.cred = X509Credentials(Config.certificate, Config.private_key, [Config.ca])
        self.accounting = [__import__("mediaproxy.interfaces.accounting.%s" % mod.lower(), globals(), locals(), [""]).Accounting() for mod in set(Config.accounting)]
        self.cred.verify_peer = True
        self.relay_factory = RelayFactory(self)
        dispatcher_addr, dispatcher_port = Config.listen
        self.relay_listener = reactor.listenTLS(dispatcher_port, self.relay_factory, self.cred, interface=dispatcher_addr)
        self.openser_factory = OpenSERControlFactory(self)
        self.openser_listener = reactor.listenUNIX(Config.socket, self.openser_factory)

    def run(self):
        process.signals.add_handler(signal.SIGHUP, self._handle_SIGHUP)
        process.signals.add_handler(signal.SIGINT, self._handle_SIGINT)
        process.signals.add_handler(signal.SIGTERM, self._handle_SIGTERM)
        for act in self.accounting:
            act.start()
        reactor.run(installSignalHandlers=False)

    def send_command(self, command, headers):
        return maybeDeferred(self.relay_factory.send_command, command, headers)

    def update_statistics(self, stats):
        log.debug("Got statistics: %s" % stats)
        for act in self.accounting:
            act.do_accounting(stats)

    def _handle_SIGHUP(self, *args):
        log.msg("Received SIGHUP, shutting down.")
        reactor.callFromThread(self._shutdown)

    def _handle_SIGINT(self, *args):
        if process._daemon:
            log.msg("Received SIGINT, shutting down.")
        else:
            log.msg("Received KeyboardInterrupt, exiting.")
        reactor.callFromThread(self._shutdown)

    def _handle_SIGTERM(self, *args):
        log.msg("Received SIGTERM, shutting down.")
        reactor.callFromThread(self._shutdown)

    def _shutdown(self):
        defer = DeferredList([result for result in [self.openser_listener.stopListening(), self.relay_listener.stopListening()] if result is not None])
        defer.addCallback(lambda x: self.openser_factory.shutdown())
        defer.addCallback(lambda x: self.relay_factory.shutdown())
        defer.addCallback(lambda x: self._stop())

    def _stop(self):
        for act in self.accounting:
            act.stop()
        reactor.stop()
