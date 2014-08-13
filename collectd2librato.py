#!/usr/bin/env python

# collectd2librato.py - Arno Hautala <arno@alum.wpi.edu>
#   This work is licensed under a Creative Commons Attribution-ShareAlike 3.0 Unported License.
#   (CC BY-SA-3.0) http://creativecommons.org/licenses/by-sa/3.0/

import \
    argparse, \
    atexit, os, re, sys, subprocess, \
    errno, signal, \
    json, hashlib, \
    datetime, time, \
    urllib2, base64, math, \
    logging, logging.handlers

from collections import defaultdict
from Queue import Queue
from threading import Thread

import pyrrd
from pyrrd.rrd import DataSource, RRA, RRD, external

_orig_prepareObject = external.prepareObject
def _monkeypatch_rrd_prepareObject(function, obj):
    ret = _orig_prepareObject(function, obj)
    if function != 'fetch':
        return ret
    filename, cf, params = ret
    return (filename, [cf] + params)
external.prepareObject = _monkeypatch_rrd_prepareObject

spaces = re.compile(" +")

def query_num_procs():
    return int(subprocess.check_output(["/sbin/sysctl", "-n", "hw.ncpu"]))

def handle_proc(proc_id):
    return float(subprocess.check_output(["/sbin/sysctl", "-n", "dev.cpu."+str(proc_id)+".temperature"]).replace('C',''))

def handle_drive(drive_id):
    output = subprocess.check_output(["/usr/local/sbin/smartctl", "-A", drive_id])
    for line in output.splitlines():
        if "Temperature_Celsius" not in line:
            continue
        return float(spaces.split(line)[9])

def check_pid(pid):        
    """ Check For the existence of a unix pid. """
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    else:
        return True

def build_librato_request(user, token, payload):
    base64string = base64.encodestring('%s:%s' % (user, token)).replace('\n', '')

    request = urllib2.Request('https://metrics-api.librato.com/v1/metrics')
    request.add_header('Authorization', 'Basic %s' % base64string)
    request.add_header('Content-Type', 'application/json')

    return {'request':request, 'data':payload}

def rrd_path_to_name(rrd_path):
    return rrd_path.replace(args.prefix,'').replace('/',':').replace('.rrd','')

class TimeoutException(Exception):
    pass

class CollectdLibrato:
    def __init__(self, pid, *args, **kwargs):
        self.librato_thread = None
        self.librato_queue = None
                
        self.last_sent = defaultdict(int)
        
        self.running = True
        
    def handle_prefix(self, prefix):
        for root, dirs, files in os.walk(prefix):
            for f in files:
                if not f.endswith('.rrd'):
                    continue
                self.handle_rrd('{}/{}'.format(root, f))
        self.librato_queue.put(None)

    def handle_rrd(self, rrd_path):
        rrd_name = rrd_path_to_name(rrd_path)
        
        if rrd_name not in args.gauges and not args.all_gauges:
            return False
        
        try:
            if args.debug:
                logging.debug('reading: '+rrd_path)
            rrd = RRD(rrd_path, mode='r')
        except pyrrd.exceptions.ExternalCommandError as e:
            logging.error('error:'+rrd_path)
            logging.error('command: '+str(e))
            return False
        
        if args.debug:
            logging.debug('fetch')
        fetch = rrd.fetch()
        
        if args.debug:
            logging.debug('got: '+str(len(fetch.keys())))
            
        for key in fetch.keys():
            data = [i for i in rrd.fetch()[key] if not math.isnan(i[1])]
            metric_name = rrd_name
            if key != 'value':
                metric_name = metric_name+':'+key
            last = 0
            for item in data:
                if len(item) is not 2:
                    logging.warning('dropped:'+str(drop_count))
                    logging.warning(metric_name+": "+str(len(item)))
                    continue
                if item[0] < int(time.time()) - 15*60:
                    continue
                if item[0] < self.last_sent[metric_name]:
                    continue
                record = {
                    'name':         metric_name,
                    'source':       args.hostname,
                    'measure_time': item[0],
                    'value':        item[1],
                    'tries':        0
                }
                self.librato_queue.put(record)
        return True

    def handle_drive_temperature(self, drives):
        for drive_class in drives:
            for id in drive_class["ids"]:
                drive_id = drive_class["type"]+str(id)
                record = {
                    'name':         "disk-"+drive_id+":temperature",
                    'source':       'tardis',
                    'measure_time': time.time(),
                    'value':        handle_drive(drive_class["prefix"]+drive_id),
                    'tries':        0
                }
                self.librato_queue.put(record)
                
    def handle_processor_temperature(self):
        for number in range(int(query_num_procs())):
            record = {
                'name':         "cpu"+str(number)+":temperature",
                'source':       'tardis',
                'measure_time': time.time(),
                'value':        handle_proc(number),
                'tries':        0
            }
            self.librato_queue.put(record)
            
    def librato_worker(self):
        metrics = list()
        while self.running:
            metric = self.librato_queue.get()
            if metric == 'last':
                metric = None
                self.running = False
            if metric:
                metric['tries'] += 1
                metrics.append(metric)
            if len(metrics) >= args.max_request_count or not metric:
                if not metrics and not metric:
                    self.librato_queue.task_done()
                    continue
                logging.info('sending: %(sending)i, %(queue)i still in queue', {"sending":len(metrics), "queue":self.librato_queue.qsize()})
                success = self.send_metrics(metrics)
                if args.debug:
                    logging.debug('result: '+success)
                if not success:
                    drop_count = 0
                    for metric in metrics:
                        if metric['tries'] < args.max_tries:
                            self.librato_queue.put(metric)
                        else:
                            drop_count += 1
                    logging.warn('dropped: %(count)i', {"count":drop_count})
                for metric in metrics:
                    self.last_sent[metric['name']] = max(self.last_sent[metric['name']], metric['measure_time'])
                    self.librato_queue.task_done()
                metrics = list()
                time.sleep(1)
                
    def send_metrics(self, metrics):
        request = build_librato_request(config['librato']['user'], config['librato']['token'], { 'gauges': metrics })
        response = False
        try:
            response = urllib2.urlopen(request['request'], json.dumps(request['data']))
        except urllib2.HTTPError, e:
            return False
        except BadStatusLine, line:
            return False
        if not response:
            return False
        if 200 == response.code:
            return True
        else:
            return False
            
    def run(self):
        self.librato_queue = Queue()
        self.librato_thread = Thread(target=self.librato_worker, args=())
        self.librato_thread.start()
        
        while self.running:
            logging.info("loop")
            logging.info("handle rrd: %(prefix)s", {"prefix":args.prefix})
            self.handle_prefix(args.prefix)
            logging.info("handle drives: %(drives)s", {"drives":args.drives})
            self.handle_drive_temperature(args.drives)
            logging.info("handle procs:")
            self.handle_processor_temperature()
            logging.info("sleep")
            try:
                time.sleep(args.interval)
            except KeyboardInterrupt:
                logging.error("keyboard")
                self.running = False
                self.librato_queue.put(None)

    def test(self):
        logging.info("starting")
        self.librato_queue = Queue()
        self.librato_thread = Thread(target=self.librato_worker, args=())
        self.librato_thread.start()
        
        logging.info("handle rrd: %(prefix)s", {"prefix":args.prefix})
        self.handle_prefix(args.prefix)
        logging.info("handle drives: %(drives)s", {"drives":args.drives})
        self.handle_drive_temperature(args.drives)
        logging.info("handle procs:")
        self.handle_processor_temperature()
        logging.info('finishing')
        self.librato_queue.put('last')
        self.librato_thread.join()
        
class VAction(argparse.Action):
    def __call__(self, parser, args, values, option_string=None):
        if values==None:
            values='1'
        try:
            values=int(values)
        except ValueError:
            values=values.count('v')+1
        setattr(args, self.dest, values)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Parse rrd files and upload to Librato.')
    parser.add_argument('gauges', action='append', nargs='*', help='a gauge name to monitor')
    parser.add_argument('-a', '--all-gauges', dest='all_gauges', action='store_true', default=False, help='monitor all gauge names')
    parser.add_argument('-p', '--pid', dest='pid', default='/var/run/collectd_librato.pid', help='specify the pid file')
    parser.add_argument('-c', '--config', dest='config', help='specify the configuration file')
    parser.add_argument('-d', '--prefix',  dest='prefix', help='specify the prefix directory where data will be logged')
    parser.add_argument('-n', '--dry-run', dest='dryrun', action='store_true', default=False, help='just monitor, do not actually upload to Librato')
    parser.add_argument('-v', '--verbose', dest='verbose', action=VAction, nargs='?', default=0, help='increase verbosity')
    parser.add_argument('--debug',   dest='debug', action=VAction, nargs='?', default=0, help='increase verbosity to debug levels')
    parser.add_argument('--run',     dest='action', action='store_const', const='run',     default='start', help='run the service continuously')
    parser.add_argument('--test',    dest='action', action='store_const', const='test',    default='start', help='test by running over the files once')

    args = parser.parse_args()
    args.gauges[:] = args.gauges[0]
    
    # default logger to console
    logging.basicConfig(
        format='%(asctime)s %(name)-12s %(levelname)-8s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    if not os.path.isdir(args.prefix):
        logging.error('prefix dir does not exist: "'+args.prefix+'"')
        parser.print_usage()
        sys.exit(2)
    if not args.config:
        logging.error('configuration file not specified')
        parser.print_usage()
        sys.exit(2)
    if not os.path.isfile(args.config):
        logging.error('configuration file does not exist: "'+args.config+'"')
        parser.print_usage()
        sys.exit(2)

    if (args.config.endswith('.json')):
        json_data=open(args.config)
        config = json.load(json_data)
        json_data.close()
            
        if (not args.gauges and not args.all_gauges):
            args.gauges = config['gauges']
        
        args.hostname = config['hostname']
        args.max_tries = config['max_tries']
        args.interval = config['interval']
        args.drives = config['drives']
        args.max_request_count = config['max_request_count']

    logger = logging.getLogger('')

    if 'log_path' in config:
        handler = logging.handlers.WatchedFileHandler(config['log_path'])
        handler.setFormatter(logging.Formatter(
            fmt='%(asctime)s %(name)-12s %(levelname)-8s %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        ))
        logger.addHandler(handler)

    if args.debug > 0:
        args.verbose += 4

    if args.verbose == 0:
        logger.setLevel(logging.CRITICAL)
    elif args.verbose == 1:
        logger.setLevel(logging.ERROR)
    elif args.verbose == 2:
        logger.setLevel(logging.WARNING)
    elif args.verbose == 3:
        logger.setLevel(logging.INFO)
    elif args.verbose == 4:
        logger.setLevel(logging.DEBUG)
    elif args.verbose >= 5:
        logger.setLevel(logging.NOTSET)

    service = CollectdLibrato(args.pid, verbose=args.verbose)

    action = None
    try:
        action = getattr(service, args.action)
    except AttributeError:
        logging.error('Unknown command: "'+args.action+'"')
        parser.print_usage()
        sys.exit(2)
    try:
        action()
    except KeyboardInterrupt:
        logging.error('stopping: "'+args.action+'"')
        service.running = False
        service.librato_queue.put(None)

    sys.exit(0)
