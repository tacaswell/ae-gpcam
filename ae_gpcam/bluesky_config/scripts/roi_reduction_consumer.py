import argparse
import asyncio
import numpy as np
import pickle
import pprint
import time

from event_model import RunRouter
from event_model import DocumentRouter
from event_model import compose_run
#from bluesky.callbacks.zmq import RemoteDispatcher
from bluesky.run_engine import Dispatcher, DocumentNames
from bluesky.callbacks.zmq import Publisher as zmqPublisher

from databroker import Broker


class RemoteDispatcher(Dispatcher):
    """
    Dispatch documents received over the network from a 0MQ proxy.
    Parameters
    ----------
    address : tuple
        Address of a running 0MQ proxy, given either as a string like
        ``'127.0.0.1:5567'`` or as a tuple like ``('127.0.0.1', 5567)``
    prefix : bytes, optional
        User-defined bytestring used to distinguish between multiple
        Publishers. If set, messages without this prefix will be ignored.
        If unset, no mesages will be ignored.
    loop : zmq.asyncio.ZMQEventLoop, optional
    zmq : object, optional
        By default, the 'zmq' module is imported and used. Anything else
        mocking its interface is accepted.
    zmq_asyncio : object, optional
        By default, the 'zmq.asyncio' module is imported and used. Anything
        else mocking its interface is accepted.
    deserializer: function, optional
        optional function to deserialize data. Default is pickle.loads
    Examples
    --------
    Print all documents generated by remote RunEngines.
    >>> d = RemoteDispatcher(('localhost', 5568))
    >>> d.subscribe(print)
    >>> d.start()  # runs until interrupted
    """
    def __init__(self, address, *, prefix=b'',
                 loop=None, zmq=None, zmq_asyncio=None,
                 deserializer=pickle.loads):
        if isinstance(prefix, str):
            raise ValueError("prefix must be bytes, not string")
        if b' ' in prefix:
            raise ValueError("prefix {!r} may not contain b' '".format(prefix))
        self._prefix = prefix
        if zmq is None:
            import zmq
        if zmq_asyncio is None:
            import zmq.asyncio as zmq_asyncio
        if isinstance(address, str):
            address = address.split(':', maxsplit=1)
        self._deserializer = deserializer
        self.address = (address[0], int(address[1]))

        if loop is None:
            loop = zmq_asyncio.ZMQEventLoop()
        self.loop = loop
        asyncio.set_event_loop(self.loop)
        self._context = zmq_asyncio.Context()
        self._socket = self._context.socket(zmq.SUB)
        url = "tcp://%s:%d" % self.address
        self._socket.connect(url)
        self._socket.setsockopt_string(zmq.SUBSCRIBE, "")
        self._task = None
        self.closed = False

        super().__init__()

    async def _poll(self):
        our_prefix = self._prefix  # local var to save an attribute lookup
        while True:
            message = await self._socket.recv()
            prefix, name, doc = message.split(b' ', 2)
            name = name.decode()
            if (not our_prefix) or prefix == our_prefix:
                try:
                    doc = self._deserializer(doc)
                    self.loop.call_soon(self.process, DocumentNames[name], doc)
                except Exception as e:
                    print(f"something bad happened with a {name} document")
                    print(e)

    def start(self):
        if self.closed:
            raise RuntimeError("This RemoteDispatcher has already been "
                               "started and interrupted. Create a fresh "
                               "instance with {}".format(repr(self)))
        try:
            self._task = self.loop.create_task(self._poll())
            self.loop.run_forever()
        except BaseException:
            self.stop()
            raise

    def stop(self):
        if self._task is not None:
            self._task.cancel()
            self.loop.stop()
        self._task = None
        self.closed = True


class ROIPicker(DocumentRouter):
    def __init__(self, publisher, peak_location):
        self._pub = publisher
        self.desc_bundle = None
        self._peak_location = peak_location
        self._data = None
        self._databroker = Broker.named("xpd")

    def start(self, doc):
        self._source_uid = doc["original_start_uid"]
        self._sample_name = doc.get("sample_name", None)
        self.start_bundle = compose_run(
            metadata=dict(raw_uid=self._source_uid, integrated_uid=doc["uid"], batch_count=doc.get("batch_count", None))
        )
        self._pub("start", self.start_bundle.start_doc)

    def event_page(self, doc):
        print(f"event_page: {pprint.pformat(doc)}")
        if self.desc_bundle is None:
            self.desc_bundle = self.start_bundle.compose_descriptor(
                name="primary",
                data_keys={
                    "I_00": {
                        "dtype": "number",
                        "source": "computed",
                        "units": "arb",
                        "shape": [],
                    },
                    "Q_00": {
                        "dtype": "number",
                        "source": "computed",
                        "units": "arb",
                        "shape": [],
                    },
                    "ctrl_Ti": {
                        "dtype": "number",
                        "source": "computed",
                        "units": "arb",
                        "shape": [],
                    },
                    "ctrl_annealing_time": {
                        "dtype": "number",
                        "source": "computed",
                        "units": "arb",
                        "shape": [],
                    },
                    "ctrl_temp": {
                        "dtype": "number",
                        "source": "computed",
                        "units": "arb",
                        "shape": [],
                    },
                },
            )
            self._pub("descriptor", self.desc_bundle.descriptor_doc)
            self._data = []
        peak_location = self._peak_location
        out = []
        # TODO look this up!
        # It appears that xpdan does not propogate additional keys, so we will
        # need to reach back into databroker to pull out the raw data!
        orig_uid = self._source_uid
        # if the sample name is here, parse it.  This works for data from the
        # last run, not sure if it will work in the future.
        if self._sample_name is not None:
            md = parse_name(self._sample_name)
            print(f"what's in {self._sample_name}: {pprint.pformat(md)}")
            print(f"?event_page doc: {pprint.pformat(doc)}")
            ti = 1.0 # doc["data"]["ctrl_Ti"]
            at = 2 # int(doc["data"]["anneal_time"] * 60)
            temp = 3.0 # doc["data"]["temp"]
        else:
            # TODO look up in db to get actual values xpdan does not forward
            # extra fields
            ti = 4.0 # doc["data"]["ctrl_Ti"]  # 0.5
            at = 5 # doc["data"]["ctrl_annealing_time"]  # 5
            temp = 6.0 # doc["data"]["ctrl_temp"]  # 450

        for Q, I in zip(doc["data"]["q"], doc["data"]["mean"]):

            data = {
                "I_00": compute_peak_area(Q, I, *peak_location),
                # pick the center of the peak as the Q
                "Q_00": np.mean(peak_location),
                # mirror out the control values
                "ctrl_Ti": ti,
                "ctrl_annealing_time": at,
                "ctrl_temp": temp,
            }
            self._data.append(data)

            # import matplotlib.pyplot as plt
            #
            # plt.plot(Q, I, "-x")
            # plt.axvspan(*peak_location, color="k", alpha=0.5)
            # plt.show()

        print(f"out: {out}")

    def stop(self, doc):
        print(f"stop document arrived")
        _ts = time.time()
        print(f"len(self._data): {len(self._data)}")
        if len(self._data):
            keys = list(self._data[0])
            data = {k: np.mean([d[k] for d in self._data]) for k in keys}
            data["I_00_variance"] = np.var([d["I_00"] for d in self._data])
            ts = {k: _ts for k in data}
            self._pub("event", self.desc_bundle.compose_event(data=data, timestamps=ts))

        stop_doc = self.start_bundle.compose_stop()
        self._pub("stop", stop_doc)


def compute_peak_area(Q, I, q_start, q_stop):
    """
    Integrated area under a peak with estimated background removed.

    Estimates the background by averaging the 3 values on either side
    of the peak and subtracting that as a constant from I before
    integrating.

    Parameters
    ----------
    Q, I : array
        The q-values and binned intensity.  Assumed to be same length.

    q_start, q_stop : float
        The region of q to integrate.  Must be in same units as the Q.

    Returns
    -------
    peak_area : float

    """

    # figure out the index of the start and stop of the q
    # region of interest
    start, stop = np.searchsorted(Q, (q_start, q_stop))
    # add one to stop because we want the index after the end
    # value not the one before
    stop += 1
    # pull out the region of interest from I.
    data_section = I[start:stop]
    # pull out one more q value than I because we want the bin widths.
    q_section = Q[start : stop + 1]
    # compute width of each of the Q bins.
    dQ = np.diff(q_section)
    # estimate the background level by averaging the 3 and and 3 I(q) outside of
    # our ROI in either direction.
    background = (np.mean(I[start - 3 : start]) + np.mean(I[stop : stop + 3])) / 2
    # do the integration!
    return np.sum((data_section - background) * dQ)


def parse_name(inp):
    # special case the empty sample position
    if "empty" in inp:
        return None
    # the sample name can have 3 or 4 _
    try:
        comp, temp, time, batch, coord_number = inp.split("_")
    except ValueError:
        try:
            comp, pristene, batch, coord_number = inp.split("_")
            # if the sample is "pristene" default to room temperature and 0 cooking
            if pristene == "Pristine":
                temp = "25C"
                time = "0min"
            else:
                return None
        except ValueError:
            return None
    # TODO check the post fixes are correct
    time = float(time.replace("p", ".")[:-3])
    temp = float(temp[:-1])
    comp = {comp[:2]: int(comp[2:4]), comp[4:6]: int(comp[6:])}
    return {
        **comp,
        "temp": temp,
        "anneal_time": time,
        "batch": tuple(map(int, batch.split("-"))),
        "position": tuple(map(int, coord_number.split("-"))),
    }


def xpdan_result_picker_factory(zmq_publisher, peak_location):
    def xpdan_result_picker(name, start_doc):
        print(f"analysis stage: {start_doc.get('analysis_stage')}")
        if start_doc.get("analysis_stage", "") == "integration":
            print(f"got integration start document")
            return [ROIPicker(zmq_publisher, peak_location)], []
        return [], []

    return xpdan_result_picker


def womp_womp(docp):
    """need this to work around xpdan putting diffpy objects in some events"""

    import pickle

    try:
        doc = pickle.loads(docp)
    except Exception as e:
        print(e)
        return {"time": 0, "data": {}, "timestamps": {}, "uid": "", "seq_num": 0}
    return doc


arg_parser = argparse.ArgumentParser()

# publish 0MQ messages at XPD from xf28id2-ca1:5577
# subscribe to 0MQ messages at XPD from xf28id2-ca1:5578
arg_parser.add_argument("--zmq-host", type=str, default="xf28id2-ca1")
arg_parser.add_argument("--zmq-publish-port", type=int, default=5577)
arg_parser.add_argument("--zmq-publish-prefix", type=str, default="rr")
arg_parser.add_argument("--zmq-subscribe-port", type=int, default=5578)
arg_parser.add_argument("--zmq-subscribe-prefix", type=str, default="an")

args = arg_parser.parse_args()

pprint.pprint(vars(args))

# this process listens for 0MQ messages with prefix "an" (from xpdan)
d = RemoteDispatcher(
    f"{args.zmq_host}:{args.zmq_subscribe_port}",
    prefix=args.zmq_subscribe_prefix.encode(),
    #deserializer=womp_womp,
)

zmq_publisher = zmqPublisher(
    f"{args.zmq_host}:{args.zmq_publish_port}", prefix=args.zmq_publish_prefix.encode()
)
peak_location = (2.63, 2.7)
rr = RunRouter([xpdan_result_picker_factory(zmq_publisher, peak_location)])
d.subscribe(rr)

print(f"ROI REDUCTION CONSUMER IS LISTENING ON {args.zmq_subscribe_prefix.encode()}")
print(f"AND PUBLISHING ON {args.zmq_publish_prefix.encode()}")
d.start()
