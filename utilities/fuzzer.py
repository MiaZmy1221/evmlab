#!/usr/bin/env python3
# -*- coding: UTF-8 -*-
"""
Executes state tests on multiple clients, checking for EVM trace equivalence

"""
import json, sys, os, time, collections, shutil
import configparser, getpass
import signal
import argparse, queue, threading
import select
import docker
import logging

from evmlab import vm as VMUtils
from evmlab.tools.statetests.templates import statetest

logger = logging.getLogger(__name__)


class Config(object):

    def __init__(self, cmdline_args = None):
        """Parses 'statetests.ini'-file, which
        may contain user-specific configuration

        Note: cmdline overrides statetests.ini!
        """
        self.cmdline_args = cmdline_args  # preserve cmdline args
        self._config = configparser.ConfigParser()  # keep the loaded configfile
        self._config.read(self.cmdline_args.configfile or "statetests.ini") # load statetests.ini

        # set default section to user-profile name if available
        uname = getpass.getuser()
        if uname not in self._config.sections():
            uname = "DEFAULT"

        # A list of clients-tuples: name , isDocker, path
        self.active_clients = []
        for c in self._config[uname]['clients'].split(","):
            key = "{}.binary".format(c)
            if key in self._config[uname]:
                self.active_clients.append((c, False, self._config[uname][key]))
            key = "{}.docker_name".format(c)
            if key in self._config[uname]:
                self.active_clients.append((c, True, self._config[uname][key]))


        self.fork_config = self._config.get(uname, 'fork_config', fallback="")

        def resolve(path):
            path = path.strip()
            path = os.path.expanduser(path)
            path = os.path.abspath(path)
            return path

        # Artefacts are where stuff is stored
        self.artefacts = resolve(self._config.get(uname,'artefacts'))
        # temp paths is where we put stuff we don't necessarily save
        self.temp_path = resolve(self._config.get(uname,'tests_path'))

        # here = os.path.dirname(os.path.realpath(__file__))
        self.host_id = "%s-%s-%d" % (uname, time.strftime("%a_%H_%M_%S"), os.getpid())

        ## override configfile settings from cmdline if set
        # map argparse variables to configfile vars in section [DEFAULT]
        # argparse preference.
        for arg, value in vars(self.cmdline_args).items():
            if value is not None:
                self._config.set(uname, arg, str(value))

        self.force_save = self._config.get(uname, 'force_save', fallback=False)
        self.enable_reporting = self._config.get(uname, 'enable_reporting', fallback=False)
        self.docker_force_update_image = self._config.get(uname, 'docker_force_update_image', fallback=None)

        # expose default section
        self.default = self._config[uname]

        # expose all the codegen settings
        self.codegen = self._config["codegen"] if self._config.has_section("codegen") else None

        # expose all statetest settings
        self.statetest = self._config["statetest"] if self._config.has_section("statetest") else None

        ## --- init ---
        logger.info("config: using default: %s" % uname)
        logger.info("\n".join(self.info))

        # print all the settings
        for csect, cvals in self._config._sections.items():
            logger.debug("[%s]" % csect)
            logger.debug("\n   " + '\n   '.join("%s = %s"%(k,v) for k,v in cvals.items()))

        logger.debug("making artefacts, testfiles and logfiles dirs..")
        os.makedirs(self.artefacts, exist_ok=True)
        os.makedirs(self.testfilesPath, exist_ok=True)
        os.makedirs(self.logfilesPath, exist_ok=True)

    @property
    def testfilesPath(self):
        return "%s/testfiles/" % self.temp_path

    @property
    def logfilesPath(self):
        return "%s/logs/" % self.temp_path

    @property
    def clientNames(self):
        return [name for (name, y, z) in self.active_clients]

    @property
    def info(self):
        out = []
        out.append("Active clients:")
        for (name, isDocker, path) in self.active_clients:
            out.append("  * {} : {} docker:{}".format(name, path, isDocker))

        out.append("Test generator: native (py)")
        out.append("Fork config:   %s" % self.fork_config)
        out.append("Artefacts:     %s" % self.artefacts)
        out.append("Tempfiles:     %s" % self.temp_path)
        out.append("Log path:      %s" % self.logfilesPath)
        out.append("Test files:    %s" % self.testfilesPath)
        return out


class RawStateTest(object):

    def __init__(self, statetest, identifier, filename, config):
        self.identifier = identifier
        self._filename = filename
        self.statetest = statetest
        self.canon_traces = []
        self.procs = []
        self.traceFiles = []
        self.additionalArtefacts = []
        self._config = config

    @property
    def filename(self):
        return self._filename

    @property
    def id(self):
        return self.identifier

    @property
    def fullfilename(self):
        return os.path.abspath("%s/%s" % (self._config.testfilesPath, self.filename))

    def writeToFile(self):
        # write to unique tmpfile
        logger.debug("Writing file %s" % self.fullfilename)
        with open(self.fullfilename, 'w') as outfile:
            json.dump(self.statetest, outfile)

    def removeFiles(self):
        f = self.fullfilename
        logger.info("Removing test artefacts %s" % ([f] + self.traceFiles))
        os.remove(f)
        # delete non-failed traces
        for f in self.traceFiles:
            os.remove(f)

    def tempTraceFilename(self, client):
        return "%s-%s.trace.log" % (self.identifier, client)

    def tempTraceLocation(self, client):
        return os.path.abspath("%s/%s" % (self._config.logfilesPath,self.tempTraceFilename(client)))

    def storeTrace(self, client, command):
        filename = self.tempTraceLocation(client)
        logger.debug("%s full trace %s saved to %s" % (client, self.id, filename))
        #       Not needed when docker-processes write directly into files
        #        with open(filename, "w+") as f:
        #            f.write("# command\n")
        #            f.write("# %s\n\n" % command)
        #            f.write(output)
        #
        self.traceFiles.append(filename)

    def saveArtefacts(self):
        # Save the actual test json
        saveloc = "%s/%s" % (self._config.artefacts, self.filename)
        logger.info("Saving testcase as %s", saveloc)
        shutil.move(self.fullfilename, saveloc)

        newTracefiles = []

        for f in self.traceFiles:
            fname = os.path.basename(f)
            newloc = "%s/%s" % (self._config.artefacts,fname)
            logger.info("Saving trace as %s", newloc)
            shutil.move(f, newloc)
            newTracefiles.append(newloc)

        self.traceFiles = newTracefiles

    def addArtefact(self, fname, data):
        fullpath = "%s/%s-%s" % (self._config.artefacts, self.id, fname)
        logger.info("Saving artefact %s", fullpath)
        with open(fullpath, "w+") as f:
            f.write(data)
        self.additionalArtefacts.append(fullpath)

    def listArtefacts(self):
        return {
            "id": self.id,
            "file": self.filename,
            "traces": [os.path.basename(f) for f in self.traceFiles],
            "other": [os.path.basename(f) for f in self.additionalArtefacts],
        }


class StateTest(RawStateTest):
    """ This class represents a single statetest, with a single post-tx result: one transaction
    executed on one single fork
    """

    def __init__(self, statetest, counter, config, overwriteFork=True):
        self.number = None
        identifier = "%s-%d" %(config.host_id, counter)
        filename = "%s-test.json" % identifier
        super().__init__(statetest, identifier, filename, config=config)


        if overwriteFork and "Byzantium" in statetest['randomStatetest']['post'].keys():
            # Replace the fork with what we are currently configured for
            postState = statetest['randomStatetest']['post'].pop('Byzantium')
            statetest['randomStatetest']['post'][self._config.fork_config] = postState


            # Replace the top level name 'randomStatetest' with something meaningful (same as filename)
        statetest['randomStatetest%s' % self.identifier] = statetest.pop('randomStatetest', None)

        self.statetest = statetest
        self.canon_traces = []
        self.procs = []
        self.traceFiles = []
        self.additionalArtefacts = []


class TestExecutor(object):

    def __init__(self, fuzzer):
        self._fuzzer = fuzzer

        self.stats = {
            "pass_count": 0,
            "fail_count": 0,
            "start_time": time.time(),
            "total_count": 0,
            "num_active_tests": 0,
            "num_active_sockets": 0,
        }
        self.failures = []
        self.traceLengths = collections.deque([], 100)
        self.traceDepths = collections.deque([], 100)
        self.traceConstantinopleOps = collections.deque([], 100)

    def onPass(self):
        self.stats["pass_count"] = self.stats["pass_count"] + 1
        self.stats["total_count"] = self.stats["total_count"] + 1

    def onFail(self, testcase):
        self.stats["fail_count"] = self.stats["fail_count"] + 1
        self.stats["total_count"] = self.stats["total_count"] + 1
        self.failures.append(testcase.listArtefacts())

    def numFails(self):
        return self.stats["fail_count"]

    def numPass(self):
        return self.stats["pass_count"]

    def numTotals(self):
        return self.stats["total_count"]

    def testsPerSecond(self):
        return self.numTotals() / (time.time() - self.stats["start_time"])

    def postprocess_test(self, test, reporting=False):
        # End previous procs
        if test is None:
            return
        data = self._fuzzer.end_processes(test)
        if data is not None:
            (traceLength, stats) = data
            self.traceLengths.append(traceLength)
            self.traceDepths.append(stats['maxDepth'])
            self.traceConstantinopleOps.append(stats['constatinopleOps'])

        # Process previous traces
        failingTestcase = self._fuzzer.processTraces(test, forceSave=self._fuzzer._config.force_save)
        if failingTestcase is None:
            self.onPass()
        else:
            self.onFail(failingTestcase)

        if reporting:
            # Do some reporting
            logger.info("Fails: {}, Pass: {}, #test {} speed: {:f} tests/s (trace_len avg: {}, max: {}, zero_trace_rate: {})".format(
                self.numFails(), self.numPass(), self.numTotals(), self.testsPerSecond(),
                self._fuzzer._total_trace_len / self._fuzzer._num_traces_processed, self._fuzzer._max_trace_len, self._fuzzer._num_zero_traces/self._fuzzer._num_traces_processed
            ))

    def startFuzzing(self):
        self.stats["start_time"] = time.time()
        # This is the max cap of paralellism, it's just to prevent
        # things going out of hand if tests start piling up
        # We don't expect to actually reach it
        MAX_PARALELL = 50
        # The poller which we use, to register our
        # processes IO channels on
        poller = select.poll()
        active_sockets = {}

        # The poll-mask. We listen to everything, except 'ready to write'
        mask = select.POLLIN | select.POLLPRI | select.POLLERR | select.POLLHUP | select.POLLNVAL

        for test in self._fuzzer.generate_tests():
            test.socketEvent = ""
            test.socketData = ""
            if self.stats["num_active_tests"] < MAX_PARALELL:
                #test.writeToFile()
                # Start new procs
                self._fuzzer.start_processes(test)
                self.stats["num_active_tests"] = self.stats["num_active_tests"] + 1
                self.stats["num_active_sockets"] = len(active_sockets.keys())
                # Put the new test to the first position
                test.numprocs = 0
                # Register the test IO channel with the poller
                for (proc_info, client_name) in test.procs:
                    socket = proc_info["output"]

                    poller.register(socket, mask)
                    # Make a lookup, socket fd-> (test and socket)
                    # The poller returns only the fd, a number, we need to 
                    # remember the actual socket and the test
                    active_sockets[socket.fileno()] = (test , socket)
                    # Stash the number of processes somewhere
                    test.numprocs = test.numprocs + 1
            else:
                logger.info("Max paralellism hit -- will sleep for a bit")
                time.sleep(10)
            # Check if anyting happened
            socketlist = poller.poll()
            if len(socketlist) == 0:
                continue
            for (socketfd, event) in socketlist:
                # At least one process for this test is finished

                # Stop listeninng to this socket
                poller.unregister(socketfd)
                # Find the test
                (test, socket) = active_sockets.pop(socketfd)
                # read it, close it
                if event & (select.POLLIN| select.POLLPRI):
                    # We don't expect any data here, but we'll take a peek and stash
                    # it just in case
                    data = socket.readall()
                    test.socketData = test.socketData + str(data) 
                #Also, we'll save the event, may assist with debugging later
                test.socketEvent = test.socketEvent + ("[%d]" % event)
                socket.close()
                test.numprocs = test.numprocs - 1
                if test.numprocs == 0:
                    logger.info("All procs finished for test %s" % test.id)
                    self.stats["num_active_tests"] = self.stats["num_active_tests"] - 1
                    self.postprocess_test(test, reporting=self._fuzzer._config.enable_reporting)

    def dry_run(self):
        tstart = time.time()
        self.stats["start_time"] = tstart
        stats_after = 10

        for nr, test in enumerate(self._fuzzer.generate_tests()):
            logger.info("[*] Test #%d: generated statetest: %s"%(nr, test.fullfilename))
            if nr % stats_after == 0:
                tdiff = time.time() - tstart
                tstart = time.time()
                logger.info("%0.2f Tests/sec" % (stats_after/tdiff))


    def status(self):
        import collections, statistics
        from datetime import datetime
        return {
            "starttime": datetime.utcfromtimestamp(self.stats["start_time"]).strftime('%Y-%m-%d %H:%M:%S'),
            "pass": self.numPass(),
            "fail": self.numFails(),
            "failures": self.failures,
            "speed": self.testsPerSecond(),
            "mean": statistics.mean(self.traceLengths) if self.traceLengths else "NA",
            "stdev": statistics.stdev(self.traceLengths) if len(self.traceLengths) > 2 else "NA",
            "numZero": self.traceLengths.count(0) if self.traceLengths else "NA",
            "max": max(self.traceLengths) if self.traceLengths else "NA",
            "maxDepth": max(self.traceDepths) if self.traceDepths else "NA",
            "numConst": statistics.mean(self.traceConstantinopleOps) if self.traceConstantinopleOps else "NA",
            "activeSockets": self.stats["num_active_sockets"],
            "activeTests": self.stats["num_active_tests"],
        }


class Fuzzer(object):

    canonicalizers = {
        "geth": VMUtils.GethVM.canonicalized,
        "cpp": VMUtils.CppVM.canonicalized,
        "py": VMUtils.PyVM.canonicalized,
        "parity": VMUtils.ParityVM.canonicalized,
        "hera": VMUtils.HeraVM.canonicalized,
    }

    def __init__(self, config=None):
        self._config = config

        self._num_traces_processed = 0
        self._total_trace_len = 0
        self._max_trace_len = 0
        self._num_zero_traces = 0

        self._dockerclient = docker.from_env()

        if config.docker_force_update_image is not None:
            for image in config.docker_force_update_image:
                self.docker_remove_image(image=image, force=True)

        codegens = {}
        for engine in (statetest.rndval.RndCodeBytes, statetest.rndval.RndCodeInstr, statetest.rndval.RndCodeSmart2):
            if self._config.codegen.getboolean("engine.%s.enabled" % engine.__name__, True):  # is engine enabled?
                codegens[engine] = int(self._config.codegen.get("engine.%s.weight" % engine.__name__,
                                                                "50"))  # create engine/weight mapping

        # todo: instantiate once?
        self.statetest_template = statetest.StateTestTemplate(nonce="0x1d",
                                                              codegenerators=codegens,
                                                              fill_prestate_for_args=True,
                                                              fill_prestate_for_tx_to=True,
                                                              _config=self._config)
        self.statetest_template.info.fuzzer = "evmlab tin"

    def docker_remove_image(self, image, force=True):
        self._dockerclient.images.remove(image=image, force=force)


    def start_daemons(self):
        """ startDaemons starts docker processes for all clients. The actual execution of
        testcases is then performed via docker exec. Means that executing a specific testcase
        does not require starting a whole new docker context, instead we just reuse the existing
        docker process.
        The startDaemon basically does this:

        ```
        docker run ethereum/client-go:alltools-latest sleep 356d
        ```

        """
        daemons = []
        # Start the processes
        for (client_name, isDocker, cmd) in self._config.active_clients:
            if isDocker:
                logger.info("Starting daemon for %s : %s", client_name, cmd)
                # First, kill off any existing daemons
                self.kill_daemon(client_name)
                procinfo = self.start_daemon(client_name, cmd)
                daemons.append((procinfo, client_name))
            else:
                logger.warning("Not a docker client %s", client_name)

    def stop_daemons(self):
        # Start the processes
        for (client_name, isDocker, cmd) in self._config.active_clients:
            if isDocker:
                logger.info("Stopping daemon for %s : %s", client_name, cmd)
                self.kill_daemon(client_name)
            else:
                logger.warning("Not a docker client %s", client_name)

    def start_daemon(self, clientname, imagename):
        self._dockerclient.containers.run(image=imagename,
                                    entrypoint="sleep",
                                    command=["356d"],
                                    name=clientname,
                                    detach=True,
                                    remove=True,
                                    volumes={
                                        self._config.testfilesPath: {'bind': '/testfiles/', 'mode': "rw"},
                                        self._config.logfilesPath: {'bind': '/logs/', 'mode': "rw"},
                                    })

        logger.info("Started docker daemon %s %s" % (imagename, clientname))

    def kill_daemon(self, clientname):
        try:
            c = self._dockerclient.containers.get(clientname)
            c.kill()
            c.stop()
        except Exception as e:
            pass

    #   VMUtils.finishProc(VMUtils.startProc(["docker", "kill",clientname]))


    def generate_tests(self):
        """This method produces json-files, each containing one statetest, with _one_ poststate.
        It stores each test with a filename that is unique per user and per process, so that two
        paralell executions should not interfere with eachother.

        returns (filename, object)
        """

        # We'll offload test generation to another thread
        q = queue.Queue(maxsize = 20)
        def createATest():
            counter = 0
            while True:
                test_obj = self.statetest_template.fill(reset_prestate=True)
                s = StateTest(test_obj, counter, config=self._config)
                s.writeToFile()
                counter = counter + 1
                q.put(s, block=True)            

        t = threading.Thread(target=createATest)
        t.start()
        # And here, just pop off the queue and yield
        while True:
            yield q.get()

    def benchmark(self, method=None, duration=None):
        counter = 0

        def default_method():
            return self.statetest_template.fill(reset_prestate=True)

        method = method or default_method

        tdiffs = []

        start = time.time()
        while True:
            x0 = time.time()
            test_obj = method()
            print(test_obj["randomStatetest"]["transaction"]["to"])
            x1 = time.time()

            print("to: %s --> pre: %r" % (test_obj["randomStatetest"]["transaction"]["to"],
                                          set(test_obj["randomStatetest"]["pre"].keys())))
            s_per_test = x1-x0
            tot_per_s = counter / (x1 - start)
            print("%d %f (tot %f/s)" % (counter, s_per_test, tot_per_s))

            counter = counter + 1
            if duration:
                tdiffs.append(s_per_test)
                if x1 > start+duration:
                    break

        return sum(tdiffs)/len(tdiffs)

    def processTraces(self, test, forceSave=False):
        if test is None:
            return None

        # Process previous traces

        (equivalent, trace_output) = VMUtils.compare_traces(test.canon_traces, self._config.clientNames)

        if equivalent and not forceSave:
            test.removeFiles()
            return None

        if not equivalent:
            logger.warning("CONSENSUS BUG!!!")

        trace_summary = self.get_summary(trace_output)
        # save the state-test
        test.saveArtefacts()
        # save combined trace and abbreviated trace
        test.addArtefact("combined_trace.log", "\n".join(trace_output))
        test.addArtefact("shortened_trace.log", "\n".join(trace_summary))

        return test

    def get_summary(self, combined_trace, n=20):
        """Returns (up to) n (default 20) preceding steps before the first diff, and the diff-section
        """
        from collections import deque
        buf = deque([], n)
        index = 0
        for index, line in enumerate(combined_trace):
            if line.startswith("[!!]"):
                buf.append("\n---- [ %d steps in total before diff ]-------\n\n" % (index))
                break
            buf.append(line)

        for i in range(index, min(len(combined_trace), index + 5)):
            buf.append(combined_trace[i])

        return list(buf)

    def testSummary(self):
        """Enable this, and test by passing a trace-output via console"""
        with open(sys.argv[1]) as f:
            print("".join(self.get_summary(f.readlines())))

    def start_processes(self, test):

        starters = {'geth': self.startGeth,
                    'cpp': self.startCpp,
                    'parity': self.startParity,
                    'hera': self.startHera}

        logger.info("Starting processes for %s on test %s" % (self._config.clientNames, test.id))
        # Start the processes
        for (client_name, x, y) in self._config.active_clients:
            if client_name in starters.keys():
                procinfo = starters[client_name](test)
                test.procs.append((procinfo, client_name))
            else:
                logger.warning("Undefined client %s", client_name)

    def end_processes(self, test):
        """ End processes for the given test, slurp up the output and compare the traces
        returns the length of the canon-trace emitted (or -1)
        """
        # Handle the old processes
        if test is None:
            return None
        tracelen = 0
        canon_steps = None
        canon_trace = []
        first = True
        stats = VMUtils.Stats()
        for (proc_info, client_name) in test.procs:
            t1 = time.time()
            test.storeTrace(client_name, proc_info['cmd'])
            canonicalizer = self.canonicalizers[client_name]
            canon_steps = []
            filename = test.tempTraceLocation(client_name)
            try:
                with open(filename) as output:
                    canon_step_generator = canonicalizer(output)
                    stat_generator = stats.traceStats(canon_step_generator)
                    canon_trace = [VMUtils.toText(step) for step in stat_generator]
            except FileNotFoundError:
                # We hit these sometimes, maybe twice every million execs or so
                logger.warning("The file %s could not be found!" % filename)
                logger.warning("Socket event %s" % test.socketEvent)
                logger.warning("Socket data %s" %  str(test.socketData))
                #TODO, try to find out what happened -- if there's any output from the process
            stats.stop()
            test.canon_traces.append(canon_trace)
            tracelen = len(canon_trace)
            self._num_traces_processed += 1
            self._total_trace_len += tracelen
            self._max_trace_len = max(self._max_trace_len, tracelen)
            if tracelen==0:
                self._num_zero_traces += 1
            t2 = time.time()
            logger.info("Processed %s steps for %s on test %s, pTime:%.02f ms "
                        % (tracelen, client_name, test.identifier, 1000 * (t2 - t1)))

        # print(stats)
        # print(canon_steps)
        # print("\n".join(canon_trace))
        return (tracelen, stats.result())

    def execInDocker(self, name, cmd, stdout=True, stderr=True):
        start_time = time.time()

        # For now, we need to disable stream, since otherwise the stderr and stdout
        # gets mixed, which causes false positives.
        # This really is a bottleneck, since it means all execution will be serial instead
        # of paralell, and makes it really slow. The fix is either to fix the python docker
        # api, or make sure that parity also prints the stateroot on stderr, which currently
        # can only be read from stdout.

        # Update, now using socket, with 1>&2 (piping stdout into stderr)
        stream = False
        socket = True
        # logger.info("executing in %s: %s" %  (name," ".join(cmd)))
        container = self._dockerclient.containers.get(name)
        (exitcode, output) = container.exec_run(cmd, stream=stream, socket=socket, stdout=stdout, stderr=stderr)

        retval = {'cmd': " ".join(cmd)}

        # If stream is False, then docker soups up the output, and we just decode it once
        # when the caller wants it

        if socket:
            retval['output'] = output
        elif stream:
            # If the stream is True, then we need to iterate over output,
            # and decode each chunk
            retval['output'] = lambda: "".join([chunk.decode() for chunk in output])
        else:
            # If we're waiting for the output, just return the decoded immediately
            retval['output'] = lambda: output.decode()

        return retval

    @staticmethod
    def shWrap(cmd, output):
        """ Wraps a command in /bin/sh, with output to the given file"""
        return ["/bin/sh", "-c", " ".join(cmd) + " &> /logs/%s" % output]

    def startGeth(self, test):
        """
        With daemonized docker images, we execute basically the following

        docker exec -it ggeth2 evm --json --code "6060" run
        or
        docker exec -it <name> <command>

        """
        cmd = ["evm", "--json", "--nomemory", "statetest", "/testfiles/%s" % os.path.basename(test.filename)]
        cmd = Fuzzer.shWrap(cmd, test.tempTraceFilename('geth'))
        return self.execInDocker("geth", cmd, stdout=False)

    def startParity(self, test):
        cmd = ["/parity-evm", "state-test", "--std-json", "/testfiles/%s" % os.path.basename(test.filename)]
        # cmd = ["/bin/sh","-c","/parity-evm state-test --std-json /testfiles/%s 1>&2" % os.path.basename(test.filename)]
        cmd = Fuzzer.shWrap(cmd, test.tempTraceFilename('parity'))
        return self.execInDocker("parity", cmd)

    def startHera(self, test):
        cmd = ["/build/test/testeth",
               "-t", "GeneralStateTests", "--",
               "--vm", "hera",
               "--evmc", "evm2wasm.js=true", "--evmc", "fallback=false",
               "--singletest", "/testfiles/%s" % os.path.basename(test.tmpfile), test.name,
               ]
        return self.execInDocker("hera", cmd, stderr=False)

    def startCpp(self, test):
        # docker exec -it cpp /usr/bin/testeth -t GeneralStateTests -- --singletest /testfiles/0001--randomStatetestmartin-Fri_09_42_57-7812-0-1-test.json randomStatetestmartin-Fri_09_42_57-7812-0   --jsontrace '{ "disableStorage" : false, "disableMemory" : false, "disableStack" : false, "fullStorage" : true }'
        # docker exec -it cpp /usr/bin/testeth -t GeneralStateTests -- --singletest /testfiles/0015--randomStatetestmartin-Fri_10_15_53-13070-3-3-test.json randomStatetestmartin-Fri_10_15_53-13070-3 --jsontrace '{"disableStack": false, "fullStorage": false, "disableStorage": false, "disableMemory": false}'

        cmd = ["/usr/bin/testeth",
               "-t", "GeneralStateTests", "--",
               "--singletest", "/testfiles/%s" % os.path.basename(test.tmpfile), test.name,
               "--jsontrace", "'%s'" % json.dumps(
                {"disableStorage": True, "disableMemory": True, "disableStack": False, "fullStorage": False})
               ]
        return self.execInDocker("cpp", cmd, stderr=False)


def event_str(event):
    r = []
    if event & select.POLLIN:
        r.append('IN')
    if event & select.POLLOUT:
        r.append('OUT')
    if event & select.POLLPRI:
        r.append('PRI')
    if event & select.POLLERR:
        r.append('ERR')
    if event & select.POLLHUP:
        r.append('HUP')
    if event & select.POLLNVAL:
        r.append('NVAL')
    return ' '.join(r)


def configFuzzer():
    ### setup logging
    logger.setLevel(logging.DEBUG)

    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    ### setup cmdline parser
    parser = argparse.ArgumentParser(description='Ethereum consensus fuzzer')
    loglevels = ['CRITICAL', 'FATAL', 'ERROR', 'WARNING', 'WARN', 'INFO', 'DEBUG', 'NOTSET']
    parser.add_argument("-v", "--verbosity", default="info",
                      help="available loglevels: %s" % ','.join(l.lower() for l in loglevels))

    # <required> configuration file: statetests.ini
    parser.add_argument("-c", "--configfile", default="statetests.ini", required=True,
                        help="path to configuration file (default: statetests.ini)")
    parser.add_argument("-D", "--dry-run", default=False, action="store_true",
                        help="Simulate and print the output instead of running it with the docker backend (default: False)")
    parser.add_argument("-B", "--benchmark", default=False, action="store_true",
                        help="Benchmark test generation (default: False)")

    grp_artefacts = parser.add_argument_group('Configure Output Artefacts and Reporting')
    grp_artefacts.add_argument("-x", "--force-save", default=None, action="store_true",
                               help="Keep tracefiles/logs/testfiles for non-failing testcases (watch disk space!) (default: False)")
    grp_artefacts.add_argument("-r", "--enable-reporting", default=None, action="store_true",
                               help="Output testrun statistics (num of passes/fails and speed (default: False)")

    grp_docker = parser.add_argument_group('Docker Settings')
    grp_docker.add_argument("-y", "--docker-force-update-image", default=None, action="append",
                               help="Remove specified docker images before starting the fuzzer to force docker to download new versions of the image (default: [])")
    grp_docker = parser.add_argument_group('Docker Settings')


    ### parse args
    args = parser.parse_args()

    if args.verbosity.upper() in loglevels:
        logger.debug("setting loglevel to %s"%args.verbosity)
        args.verbosity = getattr(logging, args.verbosity.upper())
        logger.setLevel(args.verbosity)
    else:
        parser.error("invalid verbosity selected. please check --help")

    ### create fuzzer instance, pass settings and begin executing tests.

    fuzzer = Fuzzer(config=Config(args))

    if args.benchmark:
        duration = 10
        logger.info("running benchmark for new and old method")
        # benchmark old or new method?

        # benchmark new method
        logger.info("new method: %ssec duration"%duration)
        avg = fuzzer.benchmark(duration=duration)
        logger.info("new method avg generation time: %f (%f tests/s)" % (avg, 1 / avg))

        # benchmark old method
        from evmlab.tools.statetests import templates
        from evmlab.tools.statetests import randomtest
        t = templates.new(templates.object_based.TEMPLATE_RandomStateTest)

        def old_method():
            return json.loads(json.dumps(t, cls=randomtest.RandomTestsJsonEncoder))

        logger.info("old method: %ssec duration" % duration)
        avg = fuzzer.benchmark(old_method, duration=duration)
        logger.info("old method avg generation time: %f (%f tests/s)" % (avg, 1/avg))

        sys.exit(0)

    ### setup signal handler (catches ctrl+c SIGINT)
    def signal_handler(*args, **kwargs):
        logger.warning("SIGINT - Aborting execution. please stand by until the docker instances are shut down.")
        fuzzer.stop_daemons()
        logger.info("BYE BYE.")
        sys.exit(1)

    signal.signal(signal.SIGINT, signal_handler)

    return fuzzer


def main():
    # Start all docker daemons that we'll use during the execution
    fuzzer = configFuzzer()

    if fuzzer._config.default.getboolean("dry_run", False):
        logger.warning("--DRY RUN mode-- Tests are just being generated and not being executed!")
        TestExecutor(fuzzer=fuzzer).dry_run()
        return

    fuzzer.start_daemons()
    TestExecutor(fuzzer=fuzzer).startFuzzing()


if __name__ == '__main__':
    main()
