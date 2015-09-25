from ..components import Actor
import inspect
import itertools


class FuncActor(Actor):

    """Actor defined simply by a function

    Args:
        func (callable): The function to be called on actor execution
        args (list): Fixed function positional arguments
        kwargs(dict): Fixed function keyword arguments
            args and kwargs behave as functools.partial args and keywords
        outports: output port name(s)
        inports: input port name(s)
        name (str): actor name
    """

    def __init__(self, func, args=(), kwargs={}, outports=None, inports=None, name=None):
        if not name:
            name = func.__name__
        super(FuncActor, self).__init__(name=name)
        try:
            # try to derive ports from function signature
            sig = inspect.signature(func)
            return_annotation = sig.return_annotation
            # derive ports from func signature
            if inports is None:
                # filter out args (first len(args) arguments and kwargs)
                inports = (par.name for par in
                           itertools.islice(sig.parameters.values(), len(args), None)
                           if par.name not in kwargs)
            if outports is None and return_annotation is not inspect.Signature.empty:
                # if func has a return annotation, use it for outports names
                outports = return_annotation
        except ValueError:
            # e.g. numpy has no support for inspect.signature
            # --> using manual inports
            if inports is None:
                inports = ('inp', )
            elif isinstance(inports, str):
                inports = (inports, )
        # save func as attribute
        self.func = func
        self._func_args = args
        self._func_kwargs = kwargs
        # setup inports
        for name in inports:
            self.inports.append(name)
        # setup outports
        if outports is None:
            outports = ('out', )
        elif isinstance(outports, str):
            outports = (outports, )
        for name in outports:
            self.outports.append(name)

    def get_run_args(self):
        args = tuple(port.pop() for port in self.inports)
        kwargs = {'func': self.func,
                  'func_args': self._func_args,
                  'func_kwargs': self._func_kwargs,
                  'outports': tuple(port.name for port in self.outports)}
        # kwargs['connected_ports'] = list((name for name, port in self.outports.items()
        #                                   if port.isconnected()))

        return args, kwargs

    @classmethod
    def run(cls, *args, **kwargs):
        args = kwargs['func_args'] + args
        func_res = kwargs['func'](*args, **kwargs['func_kwargs'])
        outports = kwargs['outports']

        if len(outports) == 1:
            func_res = (func_res, )
        # iterate over ports and return values
        res = {name: value for name, value in zip(outports, func_res)}
        return res

    def __call__(self, *args, **kwargs):
        args = self._func_args + args
        kwargs.update(self._func_kwargs)
        return self.func(*args, **kwargs)


class Switch(Actor):

    """While loop actor
    """

    def __init__(self, name=None, condition_func=None, inner_actor=None):
        super(Switch, self).__init__(name=name)
        # self.inports.append('initial')
        self.inports.append('loop_in')
        self.outports.append('loop_out')
        self.outports.append('final')
        if condition_func is None:
            # TODO create in and out ports for condition
            raise NotImplementedError('To be implemented')
            self.condition_func = None
        else:
            self.condition_func = condition_func
        if inner_actor:
            if len(inner_actor.inports) != 1:
                raise RuntimeError("Inner actor has to have exactly one input port.")
            if len(inner_actor.outports) != 1:
                raise RuntimeError("Inner actor has to have exactly one output port.")
            self.outports['loop_out'].connect(list(inner_actor.inports._ports.values())[0])
            self.inports['loop_in'].connect(list(inner_actor.outports._ports.values())[0])

    def get_run_args(self):
        kwargs = {}
        kwargs['input_val'] = self.inports['loop_in'].pop()
        kwargs['condition_func'] = self.condition_func
        args = ()
        return args, kwargs

    @classmethod
    def run(cls, *args, condition_func=None, input_val=None):
        res = {}
        if condition_func:
            if condition_func(input_val):
                res['loop_out'] = input_val
            else:
                res['final'] = input_val
        else:
            # TODO use condition via ports
            raise NotImplementedError('To be implemented')
        return res


class ShellRunner(Actor):

    """An actor executing external command."""

    def __init__(self, base_command, name=None, binary=False, shell=False):
        super(ShellRunner, self).__init__(name=name)

        if isinstance(base_command, str):
            self.base_command = (base_command,)
        else:
            self.base_command = base_command

        self.binary = binary
        self.shell = shell
        self.inports.append('inp')
        self.outports.append('stdout')
        self.outports.append('stderr')
        self.outports.append('ret')

    def get_run_args(self):
        vals = self.inports['inp'].pop()
        if isinstance(vals, str):
            vals = (vals,)
        args = self.base_command + vals
        kwargs = {
            'shell': self.shell,
            'binary': self.binary,
        }
        return args, kwargs

    @classmethod
    def run(cls, *args, **kwargs):
        import subprocess
        import tempfile

        print(args)

        if kwargs['binary']:
            mode = "w+b"
        else:
            mode = "w+t"

        with tempfile.TemporaryFile(mode=mode) as fout, tempfile.TemporaryFile(mode=mode) as ferr:
            if kwargs['shell']:
                result = subprocess.call(' '.join(args), stdout=fout, stderr=ferr, shell=kwargs['shell'])
            else:
                result = subprocess.call(args, stdout=fout, stderr=ferr, shell=kwargs['shell'])
            fout.seek(0)
            ferr.seek(0)
            cout = fout.read()
            cerr = ferr.read()
        res = {
            'ret': result,
            'stdout': cout,
            'stderr': cerr
        }
        return res


class Sink(Actor):

    """Dumps everything
    """

    def can_run(self):
        return True

    def get_run_args(self):
        for port in self.inports:
            port.pop()
        return (), {}

    @staticmethod
    def run(*args, **kwargs):
        pass

