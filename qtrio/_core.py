"""The module holding the core features of QTrio.

Attributes:
    REENTER_EVENT_HINT: The registered event type hint for our reenter events.
    REENTER_EVENT: The QtCore.QEvent.Type enumerator for our reenter events.
"""
import contextlib
import functools
import math
import sys
import traceback
import typing

import async_generator
import attr
import outcome
import qtpy
from qtpy import QtCore
from qtpy import QtGui
from qtpy import QtWidgets
import trio

import qtrio


# https://github.com/spyder-ide/qtpy/pull/214
if qtpy.API in qtpy.PYQT5_API and not hasattr(QtCore, "SignalInstance"):
    SignalInstance = QtCore.pyqtBoundSignal
else:
    SignalInstance = QtCore.SignalInstance


REENTER_EVENT_HINT: int = QtCore.QEvent.registerEventType()
if REENTER_EVENT_HINT == -1:
    message = (
        "Failed to register the event hint, either no available hints remain or the"
        + " program is shutting down."
    )
    raise qtrio.RegisterEventTypeError(message)

REENTER_EVENT: QtCore.QEvent.Type = QtCore.QEvent.Type(REENTER_EVENT_HINT)


def create_reenter_event(fn):
    """Create a proper `QtCore.QEvent` for reentering into the Qt host loop."""
    event = QtCore.QEvent(REENTER_EVENT)
    event.fn = fn
    return event


class Reenter(QtCore.QObject):
    """A `QtCore.QObject` for handling reenter events."""

    def event(self, event: QtCore.QEvent) -> bool:
        """Qt calls this when the object receives an event."""
        event.fn()
        return False


async def wait_signal(signal: SignalInstance) -> typing.Tuple[typing.Any, ...]:
    """Block for the next emission of `signal` and return the emitted arguments.

    Warning:
        In many cases this can result in a race condition since you are unable to
        first connect the signal and then wait for it.

    Args:
        signal: The signal instance to wait for emission of.
    """
    event = trio.Event()
    result = None

    def slot(*args):
        """Receive and store the emitted arguments and set the event so we can continue.

        Args:
            args: The arguments emitted from the signal.
        """
        nonlocal result
        result = args
        event.set()

    with qtrio.connection(signal, slot):
        await event.wait()

    return result


@attr.s(auto_attribs=True, frozen=True, slots=True, eq=False)
class Emission:
    """Stores the emission of a signal including the emitted arguments.  Can be
    compared against a signal instance to check the source.

    Note:
        Each time you access a signal such as `a_qobject.some_signal` you get a
        different signal instance object so the `signal` attribute generally will not
        be the same object.  A signal instance is a `QtCore.SignalInstance` in PySide2
        or `QtCore.pyqtBoundSignal` in PyQt5.

    Attributes:
        signal: An instance of the original signal.
        args: A tuple of the arguments emitted by the signal.
    """

    signal: SignalInstance
    args: typing.Tuple[typing.Any, ...]

    def is_from(self, signal: SignalInstance) -> bool:
        """Check if this emission came from `signal`.

        Args:
            signal: The signal instance to check for being the source.
        """

        # TODO: `repr()` here seems really bad.
        if qtpy.PYQT5:
            return self.signal.signal == signal.signal and repr(self.signal) == repr(
                signal
            )
        elif qtpy.PYSIDE2:
            return self.signal == signal

        raise qtrio.QTrioException()  # pragma: no cover

    def __eq__(self, other):
        if type(other) != type(self):
            return False

        return self.is_from(signal=other.signal) and self.args == other.args


@attr.s(auto_attribs=True)
class Emissions:
    """Hold elements useful for the application to work with emissions from signals.

    Attributes:
        channel: A memory receive channel to be fed by signal emissions.
        _aclose: The send channel's `.aclose()` method.
    """

    channel: trio.MemoryReceiveChannel
    _aclose: typing.Callable[[], typing.Awaitable[typing.Any]]

    async def aclose(self):
        """Asynchronously close the send channel when signal emissions are no longer of
        interest.
        """
        return await self._aclose()


@async_generator.asynccontextmanager
async def open_emissions_channel(
    signals: typing.Collection[SignalInstance],
) -> typing.Iterator[trio.MemoryReceiveChannel]:
    """Create a memory channel fed by the emissions of the `signals`.

    Args:
        signals: A collection of signals from which :class:`Emission`s will be created.
    """

    # Infinite buffer because I don't think there's any use in storing the emission
    # info in a `slot()` stack frame rather than in the memory channel.  Perhaps in the
    # future we can implement a limit beyond which events are thrown away to avoid
    # infinite queueing.  Maybe trio.MemorySendChannel.send_nowait() instead.
    send_channel, receive_channel = trio.open_memory_channel(max_buffer_size=math.inf)

    async def slot(signal, *args):
        await send_channel.send(Emission(signal=signal, args=args))

    async with send_channel:
        async with trio.open_nursery() as nursery:
            with contextlib.ExitStack() as stack:
                for signal in signals:
                    stack.enter_context(
                        qtrio.connection(
                            signal, functools.partial(nursery.start_soon, slot, signal)
                        ),
                    )

                yield Emissions(channel=receive_channel, aclose=send_channel.aclose)


@async_generator.asynccontextmanager
async def wait_signal_context(signal: SignalInstance) -> None:
    """Connect a signal during the context and wait for it on exit.  Presently no
    mechanism is provided for retrieving the emitted arguments.

    Args:
        signal: The signal to connect to and wait for.
    """
    event = trio.Event()

    with qtrio.connection(signal=signal, slot=lambda *args, **kwargs: event.set()):
        yield
        await event.wait()


@attr.s(auto_attribs=True, frozen=True, slots=True)
class Outcomes:
    """This class holds an :class:`outcome.Outcome` from each of the Trio and the Qt
    application execution.

    Attributes:
        qt: The Qt application :class:`outcome.Outcome`
        trio: The Trio async function :class:`outcome.Outcome`
    """

    qt: typing.Optional[outcome.Outcome] = None
    trio: typing.Optional[outcome.Outcome] = None

    def unwrap(self):
        """Unwrap either the Trio or Qt outcome.  First, errors are given priority over
        success values.  Second, the Trio outcome gets priority over the Qt outcome.  If
        both are still None a :class:`NoOutcomesError` is raised.
        """

        if self.trio is not None:
            # highest priority to the Trio outcome, if it is an error we are done
            result = self.trio.unwrap()

            # since a Trio result is higher priority, we only care if Qt gave an error
            if self.qt is not None:
                self.qt.unwrap()

            # no Qt error so go ahead and return the Trio result
            return result
        elif self.qt is not None:
            # either it is a value that gets returned or an error that gets raised
            return self.qt.unwrap()

        # neither Trio nor Qt outcomes have been set so we have nothing to unwrap()
        raise qtrio.NoOutcomesError()


def run(
    async_fn: typing.Callable[[], typing.Awaitable[None]],
    *args: typing.Tuple[typing.Any, ...],
    done_callback: typing.Optional[typing.Callable[[Outcomes], None]] = None,
) -> Outcomes:
    """Run a Trio-flavored async function in guest mode on a Qt host application, and
    return the outcomes.

    Args:
        async_fn: The async function to run.
        args: Positional arguments to pass to `async_fn`.

    Returns:
        The :class:`qtrio.Outcomes` with both the Trio and Qt outcomes.
    """
    runner = Runner(done_callback=done_callback)
    runner.run(async_fn, *args)

    return runner.outcomes


def outcome_from_application_return_code(return_code: int) -> outcome.Outcome:
    """Create either an :class:`outcome.Value` in the case of a 0 `return_code` or an
    :class:`outcome.Error` with a :class:`ReturnCodeError` otherwise.

    Args:
        return_code: The return code to be processed.
    """

    if return_code == 0:
        return outcome.Value(return_code)

    return outcome.Error(qtrio.ReturnCodeError(return_code))


@attr.s(auto_attribs=True, slots=True)
class Runner:
    """This class helps run Trio in guest mode on a Qt host application.

    Attributes:

        application: The Qt application object to run as the host.  If not set before
            calling :meth:`run` the application will be created as
            `QtWidgets.QApplication(sys.argv[1:])` and
            `.setQuitOnLastWindowClosed(False)` will be called on it to allow the
            application to continue throughout the lifetime of the async function passed
            to :meth:`qtrio.Runner.run`.
        quit_application: When true, the :meth:`done_callback` method will quit the
            application when the async function passed to :meth:`qtrio.Runner.run` has
            completed.
        reenter: The `QObject` instance which will receive the events requesting
            execution of the needed Trio and user code in the host's event loop and
            thread.
        done_callback: The builtin :meth:`done_callback` will be passed to
            :func:`trio.lowlevel.start_guest_run` but will call the callback passed here
            before (maybe) quitting the application.  The :class:`outcome.Outcome` from
            the completion of the async function passed to :meth:`run` will be passed to
            this callback.
    """

    application: typing.Optional[QtGui.QGuiApplication] = None
    quit_application: bool = True

    reenter: Reenter = attr.ib(factory=Reenter)

    done_callback: typing.Optional[typing.Callable[[Outcomes], None]] = attr.ib(
        default=None
    )

    outcomes: Outcomes = attr.ib(factory=Outcomes, init=False)
    cancel_scope: trio.CancelScope = attr.ib(default=None, init=False)

    def run(
        self,
        async_fn: typing.Callable[[], typing.Awaitable[None]],
        *args,
        execute_application: bool = True,
    ) -> outcome.Outcome:
        """Start the guest loop executing `async_fn`.

        Args:
            async_fn: The async function to be run in the Qt host loop by the Trio
                guest.
            args: Arguments to pass when calling `async_fn`.
            execute_application: If True, the Qt application will be executed and this
                call will block until it finishes.

        Returns:
            If `execute_application` is true, an :class:`Outcomes` containing outcomes
            from the Qt application and `async_fn` will be returned.  Otherwise, an
            empty :class:`Outcomes`.
        """
        if self.application is None:
            self.application = QtWidgets.QApplication(sys.argv[1:])
            self.application.setQuitOnLastWindowClosed(False)

        trio.lowlevel.start_guest_run(
            self.trio_main,
            async_fn,
            args,
            run_sync_soon_threadsafe=self.run_sync_soon_threadsafe,
            done_callback=self.trio_done,
        )

        if execute_application:
            return_code = self.application.exec_()

            self.outcomes = attr.evolve(
                self.outcomes, qt=outcome_from_application_return_code(return_code),
            )

        return self.outcomes

    def run_sync_soon_threadsafe(self, fn: typing.Callable[[], typing.Any]) -> None:
        """Helper for the Trio guest to execute a sync function in the Qt host
        thread when called from the Trio guest thread.  This call will not block waiting
        for completion of `fn` nor will it return the result of calling `fn`.

        Args:
            fn: A no parameter callable.
        """
        event = create_reenter_event(fn=fn)
        self.application.postEvent(self.reenter, event)

    async def trio_main(
        self,
        async_fn: typing.Callable[[], typing.Awaitable[None]],
        args: typing.Tuple[typing.Any, ...],
    ) -> None:
        """Will be run as the main async function by the Trio guest.  It creates a
        cancellation scope to be cancelled when `QtGui.QGuiApplication.lastWindowClosed`
        is emitted.  Within this scope the application's `async_fn` will be run and
        passed `args`.

        Args:
            async_fn: The application's main async function to be run by Trio in the Qt
                host's thread.
            args: Positional arguments to be passed to `async_fn`
        """
        with trio.CancelScope() as self.cancel_scope:
            with contextlib.ExitStack() as exit_stack:
                if self.application.quitOnLastWindowClosed():
                    exit_stack.enter_context(
                        qtrio.connection(
                            signal=self.application.lastWindowClosed,
                            slot=self.cancel_scope.cancel,
                        )
                    )

                return await async_fn(*args)

    def trio_done(self, run_outcome: outcome.Outcome) -> None:
        """Will be called after the Trio guest run has finished.  This allows collection
        of the :class:`outcome.Outcome` and execution of any application provided done
        callback.  Finally, if `quit_application` was set when creating the instance
        then the Qt application will be requested to quit().

        Actions such as outputting error information or unwrapping the outcomes need
        to be further considered.
        """
        self.outcomes = attr.evolve(self.outcomes, trio=run_outcome)

        # TODO: should stuff be reported here?  configurable by caller?
        print("---", repr(run_outcome))
        if isinstance(run_outcome, outcome.Error):
            exc = run_outcome.error
            traceback.print_exception(type(exc), exc, exc.__traceback__)

        if self.done_callback is not None:
            self.done_callback(self.outcomes)

        if self.quit_application:
            self.application.quit()
