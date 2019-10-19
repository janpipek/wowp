import itertools

from . import Actor
from ..schedulers import LinearizedScheduler


class GeneratorActor(Actor):
    class PseudoDict(object):
        def __init__(self, iterator):
            self.iterator = iterator

        def items(self):
            return self.iterator

    def run(self):
        return GeneratorActor.PseudoDict(self.iterate())

    def iterate(self):
        raise NotImplementedError(
            "It is necessary to implement iterate method that yields pairs key, value")


class LineReader(GeneratorActor):
    """Sequentially put all lines in a file."""

    def __init__(self, name="line_reader", inport_name="path", outport_name="line"):
        Actor.__init__(self, name=name)
        self.inports.append(inport_name)
        self.outports.append(outport_name)
        self.inport_name = inport_name
        self.outport_name = outport_name

    def iterate(self):
        path = self.inports[self.inport_name].pop()
        with open(path, "rt") as f:
            for line in f:
                yield self.outport_name, line.strip()


class IteratorActor(GeneratorActor):
    def __init__(self, name="iterator", inport_name="collection", outport_name="item"):
        Actor.__init__(self, name=name)
        self.inports.append(inport_name)
        self.outports.append(outport_name)
        self.inport_name = inport_name
        self.outport_name = outport_name

    def iterate(self):
        collection = self.inports[self.inport_name].pop()
        for item in collection:
            yield self.outport_name, item


class Splitter(Actor):

    _system_actor = True

    def __init__(self, name="splitter", inport_name="in", multiplicity=2):
        import itertools
        Actor.__init__(self, name=name)
        self.inport_name = inport_name
        self.multiplicity = multiplicity

        self.inports.append(inport_name)
        for i in range(1, multiplicity + 1):
            self.outports.append("%s_%d" % (inport_name, i))

        self._outports_cycle = itertools.cycle(range(1, multiplicity + 1))

    def run(self, *args, **kwargs):
        value = kwargs[self.inport_name]
        i = next(self._outports_cycle)
        outport = "%s_%d" % (self.inport_name, i)
        return {outport: value}

        # TODO: Add SequentialMerger
        # TODO: Add RandomMerger


class Chain(Actor):
    """Chain of actors.

    Each of the chain link has to have just one input & output port (to be changed)
    """

    def __init__(self, name, actor_generators, **kwargs):
        """

        :param name:
        :param actor_generators: iterable of actor classes or generators
        :param kwargs:
        """
        super(Chain, self).__init__(name=name)
        if len(actor_generators) < 1:
            raise RuntimeError("Chain needs at least one item.")
        actor_generators = list(actor_generators)
        for inport in actor_generators[0](**kwargs).inports:
            self.inports.append(inport.name)
        for outport in actor_generators[-1](**kwargs).outports:
            self.outports.append(outport.name)
        # self.outports.append("out")
        self.actor_generators = actor_generators

    @staticmethod
    def chain_actors(actors):
        iter1, iter2 = itertools.tee(actors, 2)
        next(iter2)  # drop
        for first, second in zip(iter1, iter2):
            if len(first.outports) == 1 and len(second.inports) == 1:
                inport = second.inports.at(0)
                inport += first.outports.at(0)
            else:
                for outport in first.outports:
                    second.inports[outport.name] += outport
            # TODO: Add some checks

    def get_run_args(self):
        kv = {port.name: port.pop() for port in self.inports}
        return (kv,), {"generators": self.actor_generators}

    @staticmethod
    def run(*args, **kwargs):
        print(args)
        inports_kv = args[0]
        actor_generators = kwargs.pop("generators")
        actors = [generator() for generator in actor_generators]
        Chain.chain_actors(actors)
        scheduler = LinearizedScheduler()
        for key, value in inports_kv.items():
            scheduler.put_value(actors[0].inports[key], value)
        scheduler.execute()
        return { port.name: port.pop() for port in actors[-1].outports }
        # return {"out": actors[-1].outports.at(0).pop()}
