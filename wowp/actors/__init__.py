from ..components import Actor
import inspect


class FuncActor(Actor):

    """Actor defined simply by a function
    """
    # TODO create a derived class instead of an instance

    def __init__(self, func, outports=None, inports=None, name=None):
        if not name:
            name = func.__name__
        super(FuncActor, self).__init__(name=name)
        # get function signature
        try:
            sig = inspect.signature(func)
            return_annotation = sig.return_annotation
            # derive ports from func signature
            if inports is None:
                inports = (par.name for par in sig.parameters.values())
            if outports is None and return_annotation is not inspect.Signature.empty:
                # if func has a return annotation, use it for outports names
                outports = return_annotation
        except ValueError:
            # e.g. numpy has no support for inspect.signature
            # --> using manual inports
            if inports is None:
                inports = ('in', )
            elif isinstance(inports, str):
                inports = (inports, )
        # save func as attribute
        self.func = func
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

    def run(self):
        # print('run funcactor')
        args = (port.pop() for port in self.inports)
        func_res = self.func(*args)

        if len(self.outports) == 1:
            func_res = (func_res,)
        # iterate over ports and return values
        res = {}
        for name, value in zip(self.outports.keys(), func_res):
            res[name] = value
        return res

    def get_args(self):
        args = tuple(port.pop() for port in self.inports)
        kwargs = {'func': self.func,
                  'outports': tuple(port.name for port in self.outports)}
        # kwargs['connected_ports'] = list((name for name, port in self.outports.items()
        #                                   if port.isconnected()))

        return args, kwargs

    @staticmethod
    def get_result(*args, **kwargs):
        func_res = kwargs['func'](*args)
        outports = kwargs['outports']

        if len(outports) == 1:
            func_res = (func_res, )
        # iterate over ports and return values
        res = {name: value for name, value in zip(outports, func_res)}
        return res

    def __call__(self, *args, **kwargs):
        return self.func(*args, **kwargs)


class LoopWhile(Actor):

    """While loop actor
    """

    def __init__(self, name=None, condition_func=None, inner_actor=None):
        super(LoopWhile, self).__init__(name=name)
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

    def run(self):
        input_val = self.inports['loop_in'].pop()
        res = {}
        if self.condition_func:
            if self.condition_func(input_val):
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
        self.inports.append('in')
        self.outports.append('stdout')
        self.outports.append('stderr')
        self.outports.append('return')

    def run(self):
        import subprocess
        import tempfile

        vals = self.inports['in'].pop()
        if isinstance(vals, str):
            vals = (vals,)
        args = self.base_command + vals
        print(args)

        if self.binary:
            mode = "w+b"
        else:
            mode = "w+t"

        with tempfile.TemporaryFile(mode=mode) as fout, tempfile.TemporaryFile(mode=mode) as ferr:
            result = subprocess.call(args, stdout=fout, stderr=ferr, shell=self.shell)
            fout.seek(0)
            ferr.seek(0)

            cout = fout.read()
            cerr = ferr.read()
        res = {
            'return': result,
            'stdout': cout,
            'stderr': cerr
        }
        return res


class Sink(Actor):

    """Dumps everything
    """

    def can_run(self):
        return True

    def run(self):
        for port in self.inports:
            port.pop()

