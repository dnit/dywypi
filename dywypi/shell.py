# encoding: utf-8
"""Shell interface for dywypi.  Allows urwid to take over the terminal and do
interesting things.
"""
# Many thanks to habnabit and aafshar, from whom I stole judiciously.
# Their implementations:
# - https://code.launchpad.net/~habnabit/+junk/urwid-protocol
# - https://bitbucket.org/aafshar/txurwid-main/src

import os

from twisted.application import service
from twisted.internet.protocol import Protocol
from twisted.internet.stdio import StandardIO
from twisted.python import log
from twisted.python.components import Adapter
import urwid
from urwid.raw_display import Screen


class UrwidDummyInput(object):
    """Fake stdin.

    The only thing we want urwid to know about stdin is that its fd is zero
    (mainly for setting cbreak).
    """
    def fileno(self):
        return 0

class ProtocolFileAdapter(Adapter):
    """Fake stdout.

    File-like object, at least as much as urwid cares, that redirects
    urwid's stdout through a protocol and ignores flushes.
    """
    def write(self, s):
        self.original.transport.write(s)

    def flush(self):
        pass


class TwistedScreen(Screen):
    """An urwid screen that speaks to a Twisted protocol, rather than mucking
    with stdin and stdout.  Much.
    """

    def __init__(self, protocol):
        self.protocol = protocol

        Screen.__init__(self)
        self.colors = 256
        self.bright_is_bold = True
        self.register_palette_entry(None, 'default', 'default')

        # Don't let urwid mess with stdin/stdout directly; give it these dummy
        # objects instead
        self._term_input_file = UrwidDummyInput()
        self._term_output_file = ProtocolFileAdapter(self.protocol)

    # Urwid Screen API

    # XXX untested
    def set_mouse_tracking(self):
        """Enable mouse tracking.

        After calling this function get_input will include mouse
        click events along with keystrokes.
        """
        self.protocol.transport.write(urwid.escape.MOUSE_TRACKING_ON)

        self._start_gpm_tracking()

    # twisted handles polling, so we don't need the loop to do it, we just
    # push what we get to the loop from dataReceived.
    def get_input_descriptors(self):
        return []

    # Do nothing here either. Not entirely sure when it gets called.
    def get_input(self, raw_keys=False):
        return

    # Private
    def _start_gpm_tracking(self):
        # TODO unclear if any of this is necessary locally
        # also it doesn't work anyway due to missing imports
        if not os.path.isfile("/usr/bin/mev"):
            return
        if not os.environ.get('TERM',"").lower().startswith("linux"):
            return
        if not Popen:
            return
        m = Popen(["/usr/bin/mev","-e","158"], stdin=PIPE, stdout=PIPE,
            close_fds=True)
        fcntl.fcntl(m.stdout.fileno(), fcntl.F_SETFL, os.O_NONBLOCK)
        self.gpm_mev = m

    def _stop_gpm_tracking(self):
        os.kill(self.gpm_mev.pid, signal.SIGINT)
        os.waitpid(self.gpm_mev.pid, 0)
        self.gpm_mev = None


class UrwidTerminalProtocol(Protocol):
    """A Protocol that passes input along from a Twisted transport into urwid's
    main loop.
    """

    def __init__(self, bridge_factory):
        self.bridge_factory = bridge_factory

    def connectionMade(self):
        self.bridge = self.bridge_factory(self)
        self.bridge.start()

    def dataReceived(self, data):
        """Pass keypresses along the bridge to urwid's main loop, which knows
        how to handle them.
        """
        self.bridge.push_input(data)


# TODO: catch ExitMainLoop somewhere
# TODO: when urwid wants to stop, need to close the connection and kill the service AND then the reactor...
# TODO: ctrl-c is apparently caught by twistd, not urwid?
class TwistedUrwidBridge(object):
    """Core of a simple bridge between Twisted and Urwid running on a local
    terminal.  Subclass this guy.
    """

    loop = None

    def __init__(self, terminal_protocol):
        self.terminal_protocol = terminal_protocol

        self.widget = self.build_toplevel_widget()
        #self.palette = self.create_urwid_palette()

        self.screen = TwistedScreen(self.terminal_protocol)
        self.loop = urwid.MainLoop(
            self.widget,
            screen=self.screen,
            event_loop=urwid.TwistedEventLoop(manage_reactor=False),
            unhandled_input=self.unhandled_input,
            palette=None,
        )

    def redraw(self):
        self.loop.draw_screen()

    # Override these guys:

    def build_toplevel_widget(self):
        """Returns the urwid widget to use as the top-level display."""
        raise NotImplementedError

    def unhandled_input(self, input):
        """Do something with unhandled keypresses."""
        print repr(input)
        pass

    # Starting and stopping urwid

    def start(self):
        self.screen.start()
        self.loop.run()

    def stop(self):
        # TODO this probably needs slightly more effort
        self.screen.stop()

    # Twisted interfacing

    def push_input(self, data):
        """Receive data from Twisted and push it into urwid's main loop.
        """
        # Emulate urwid's input handling.
        # Filter the input...
        keys = self.loop.input_filter(data, [])
        # Let urwid do some crunching to figure out escape sequences...
        keys, remainder = urwid.escape.process_keyqueue(map(ord, keys), True)
        # Send it along to the main loop...
        self.loop.process_input(keys)
        # And redraw.
        self.redraw()


class LocalUrwidService(service.Service):
    """Simple Service wrapper for a Twisted-Urwid bridge."""

    def __init__(self, bridge_factory):
        self.bridge_factory = bridge_factory
        self.log_buffer = []

    def startService(self):
        self.protocol = UrwidTerminalProtocol(self.bridge_factory)
        self.stdio = StandardIO(self.protocol)

        for args in self.log_buffer:
            self.protocol.bridge.add_log_line(*args)

    def stopService(self):
        self.stdio.loseConnection()
        del self.protocol
        del self.stdio


    def add_log_line(self, line, color):
        """I exist to allow the urwid app to take over logging."""
        try:
            add_log_line = self.protocol.bridge.add_log_line
        except AttributeError:
            self.log_buffer.append((line, color))
        else:
            add_log_line(line, color)


### DYWYPI-SPECIFIC FROM HERE

class UnselectableListBox(urwid.ListBox):
    """A ListBox that cannot receive focus."""
    _selectable = False


class DywypiShell(TwistedUrwidBridge):
    """Creates a Twisted-friendly urwid app that allows interacting with dywypi
    via a shell.
    """
    def build_toplevel_widget(self):
        self.pane = UnselectableListBox(urwid.SimpleListWalker([]))
        prompt = urwid.Edit('>>> ')
        return urwid.Pile(
            [
                self.pane,
                ('flow', prompt),
            ],
            focus_item=prompt,
        )

    def start(self):
        super(DywypiShell, self).start()

        from twisted.internet import reactor
        def mm():
            print 'interrupting you'
            reactor.callLater(2, mm)
        reactor.callLater(2, mm)

    def add_log_line(self, line, color):
        # TODO generalize this color thing in a way compatible with irc, html, ...
        self.pane.body.append(urwid.Text((color, line.rstrip())))
        self.pane.set_focus(len(self.pane.body) - 1)
        self.redraw()


class DywypiShellLogObserver(log.FileLogObserver):
    def __init__(self, shell_service):
        self.shell_service = shell_service

    def emit(self, event):
        text = log.textFromEventDict(event)
        if text is None:
            return

        # TODO pick color...

        line = "{time} [{system}] {text}\n".format(
            time=self.formatTime(event['time']),
            system=event['system'],
            text=text.replace('\n', '\n\t'),
        )

        self.shell_service.add_log_line(line, 'default')


def initialize_service(application):
    service = LocalUrwidService(DywypiShell)
    service.setServiceParent(application)

    application.setComponent(log.ILogObserver, DywypiShellLogObserver(service).emit)
