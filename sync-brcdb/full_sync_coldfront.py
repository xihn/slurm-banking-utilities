#!/usr/bin/python
import os
import time
import urllib2
import urllib
import json
import datetime
import calendar
import subprocess
import logging
import argparse


timestamp_format_complete = '%Y-%m-%dT%H:%M:%S'
timestamp_format_minimal = '%Y-%m-%d'
docstr = '''
Full Sync jobs between MyBRC-DB with Slurm-DB.
'''


parser = argparse.ArgumentParser(description=docstr)
parser.add_argument('--target', dest='target',
                    help='API endpoint to hit. NOTE: this url should end with a "/", example: https://mybrc.brc.berkeley.edu/api/',
                    default='https://mybrc.brc.berkeley.edu/api/')
parser.add_argument('--debug', dest='debug', action='store_true',
                    help='launch script in DEBUG mode, this will not push updates to the TARGET and write debug logs.')

parser = parser.parse_args()
DEBUG = parser.debug
BASE_URL = parser.target

LOG_FILE = 'full_sync_coldfront_debug.log' if DEBUG else 'full_sync_coldfront.log'
PRICE_FILE = '/etc/slurm/bank-config.toml'
CONFIG_FILE = 'full_sync_coldfront.conf'

logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format='%(asctime)s %(levelname)-8s %(message)s',
                    datefmt='%Y-%m-%dT%H:%M:%S')

if not os.path.exists(CONFIG_FILE):
    print('config file {} missing...'.format(CONFIG_FILE))
    logging.info('auth config file missing [{}], exiting run...'.format(CONFIG_FILE))
    exit(0)

with open(CONFIG_FILE, 'r') as f:
    AUTH_TOKEN = f.read().strip()

if DEBUG:
    print('---DEBUG RUN---')

print('starting run, using endpoint {} ...'.format(BASE_URL))
logging.info('starting run, using endpoint {} ...'.format(BASE_URL))


def calculate_cpu_time(duration, num_cpus):
    total_seconds = duration.total_seconds()
    hours = total_seconds / 3600
    return hours * float(num_cpus)


def datestring_to_utc_timestamp(dt):
    try:
        return calendar.timegm(time.strptime(dt, timestamp_format_complete))
    except:
        return calendar.timegm(time.strptime(dt, timestamp_format_minimal))


def utc_timestamp_to_string(dt):
    candidate = datetime.datetime.utcfromtimestamp(dt)
    return candidate.strftime('%Y-%m-%dT%H:%M:%SZ'), candidate


def calculate_time_duration(start, end):
    return datetime.datetime.utcfromtimestamp(datestring_to_utc_timestamp(end)) - datetime.datetime.utcfromtimestamp(datestring_to_utc_timestamp(start))


def get_price_per_hour(partition):
    lines = []
    with open(PRICE_FILE, 'r') as f:
        lines = f.readlines()

    target = 0
    partition_price_passed = False
    for line in lines:
        line = line.decode('utf-8')
        if not partition_price_passed and '[PartitionPrice]' in line:
            partition_price_passed = True
            continue

        if not partition_price_passed:
            continue

        if line[0] == '#':
            continue

        if partition in line:
            target = line.split()[-1]
            target = float(target)
            break

        if '[' in line:
            break

    if target == 0:
        target = 1
    return target


def calculate_hours(duration):
    total_seconds = duration.total_seconds()
    hours = total_seconds / 3600
    return hours


def calculate_amount(partition, cpu_count, duration):
    pphr = get_price_per_hour(partition)
    duration_hrs = calculate_hours(duration)
    cpu_count = int(cpu_count)
    return round(pphr * cpu_count * duration_hrs, 2)


def node_list_format(nodelist):
    nodes = nodelist.split(',')

    table = []
    for node in nodes:
        if '-' in node:
            extension = node.split('.')[-1]
            start, end = node.split(',')[0][1:]

            for current in range(int(start), int(end) + 1):
                current = 'n{:04d}.{}'.format(current, extension)
                table.append({"name": current})

        else:
            table.append({"name": node})

    return table


def paginate_requests(url, params=None):
    request_url = url
    params = params or {}

    if params:
        request_url = url + '?' + urllib.urlencode(params)

    try:
        req = urllib2.Request(request_url)
        response = json.loads(urllib2.urlopen(req).read())
    except urllib2.URLError as e:
        if DEBUG:
            print('[paginate_requests({}, {})] failed: {}'.format(url, params, e))
            logging.error('[paginate_requests({}, {})] failed: {}'.format(url, params, e))

        return []

    current_page = 0
    results = []
    results.extend(response['results'])
    while response['next'] is not None:
        try:
            current_page += 1
            params['page'] = current_page
            request_url = url + '?' + urllib.urlencode(params)
            req = urllib2.Request(request_url)
            response = json.loads(urllib2.urlopen(req).read())

            results.extend(response['results'])
            if current_page % 5 == 0:
                print('\tgetting page: {}'.format(current_page))

            if current_page > 50:
                print('too many pages to sync at once, rerun script after this run completes...')
                logging.warning('too many pages to sync at once, rerun script after this run completes...')
                break

        except urllib2.URLError as e:
            response['next'] = None

            if DEBUG:
                print('[paginate_requests()] failed: {}'.format(e))
                logging.error('[paginate_requests({}, {})] failed: {}'.format(url, params, e))

    return results


def single_request(url, params=None):
    request_url = url
    params = params or {}

    if params:
        request_url = url + '?' + urllib.urlencode(params)

    try:
        request = urllib2.Request(request_url)
        response = json.loads(urllib2.urlopen(request).read())
    except Exception as e:
        response = {'results': None}

        if DEBUG:
            print('[single_request({}, {})] failed: {}'.format(url, params, e))
            logging.error('[single_request({}, {})] failed: {}'.format(url, params, e))

    return response['results']


def get_project_start(project):
    allocations_url = BASE_URL + 'allocations/'
    response = single_request(allocations_url, {'project': project, 'resources': 'Savio Compute'})
    if not response or len(response) == 0:
        if DEBUG:
            print('[get_project_start({})] ERR'.format(project))
            logging.error('[get_project_start({})] ERR'.format(project))

        return None

    creation = response[0]['start_date']

    if creation:
        return creation.split('.')[0] if '.' in creation else creation
    else:
        return None


print('gathering accounts from mybrcdb...')
logging.info('gathering data from mybrcdb...')

current_month = datetime.datetime.now().month
current_year = datetime.datetime.now().year
default_start = current_year if current_month >= 6 else (current_year - 1)
default_start = '{}-06-01T00:00:00'.format(default_start)

project_table = []
project_table_unfiltered = paginate_requests(BASE_URL + 'projects/')
for project in project_table_unfiltered:
    project_name = str(project['name'])
    project_start = get_project_start(project_name)

    project['name'] = project_name
    project['start'] = default_start if not project_start else str(project_start)
    project_table.append(project)

print('gathering jobs from slurmdb...')
logging.info('gathering data from slurmdb...')

for index, project in enumerate(project_table):
    out, err = subprocess.Popen(['sacct', '-A', project['name'], '-S', project['start'],
                                 '--format=JobId,Submit,Start,End,UID,Account,State,Partition,QOS,NodeList,AllocCPUS,ReqNodes,AllocNodes,CPUTimeRAW,CPUTime', '-naPX'],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT).communicate()
    project['jobs'] = out.splitlines()

    if index % int(len(project_table) / 10) == 0:
        print('\tprogress: {}/{}'.format(index, len(project_table)))


print('parsing jobs...')
logging.info('parsing jobs...')

table = {}
for project in project_table:
    for line in project['jobs']:
        values = [str(value.decode('utf-8')) for value in line.split('|')]
        jobid, submit, start, end, uid, account, state, partition, qos, nodelist, alloc_cpus, req_nodes, alloc_nodes, cpu_time_raw, cpu_time = values

        try:
            duration = calculate_time_duration(start, end)
            node_list_converted = node_list_format(nodelist)
            cpu_time = calculate_cpu_time(duration, alloc_cpus)
            amount = calculate_amount(partition, alloc_cpus, duration)

            submit, _ = utc_timestamp_to_string(datestring_to_utc_timestamp(submit))
            start, _start = utc_timestamp_to_string(datestring_to_utc_timestamp(start))
            end, _end = utc_timestamp_to_string(datestring_to_utc_timestamp(end))

            raw_time = (_end - _start).total_seconds() / 3600

            table[jobid] = {
                'jobslurmid': jobid,
                'submitdate': submit,
                'startdate': start,
                'enddate': end,
                'userid': uid,
                'accountid': account,
                'amount': str(amount),
                'jobstatus': state,
                'partition': partition,
                'qos': qos,
                'nodes': node_list_converted,
                'num_cpus': int(alloc_cpus),
                'num_req_nodes': int(req_nodes),
                'num_alloc_nodes': int(alloc_nodes),
                'raw_time': raw_time,
                'cpu_time': float(cpu_time)}

        except Exception as e:
            logging.warning('ERROR occured for jobid: {} REASON: {}'.format(jobid, e))


if not DEBUG:
    print('updating mybrcdb with {} jobs...'.format(len(table)))
    logging.info('updating mybrcdb with {} jobs...'.format(len(table)))
else:
    print('DEBUG: collected {} jobs to update in mybrcdb...'.format(len(table)))
    logging.info('DEBUG: collected {} jobs to update in mybrcdb...'.format(len(table)))

counter = 0
for jobid, job in table.items():
    request_data = urllib.urlencode(job)
    url_target = BASE_URL + 'jobs/' + str(jobid) + '/'
    req = urllib2.Request(url=url_target, data=request_data)

    req.add_header('Authorization', 'Token ' + AUTH_TOKEN)
    req.get_method = lambda: 'PUT'

    try:
        if not DEBUG:
            json.loads(urllib2.urlopen(req).read())

        logging.info('{} PUSHED/UPDATED : {}'.format(jobid, job))
        counter += 1

        if counter % int(len(table) / 10) == 0:
            print('\tprogress: {}/{}'.format(counter, len(table)))

    except urllib2.HTTPError as e:
        logging.warning('ERROR occured for jobid: {} REASON: {}'.format(jobid, e.reason))

if not DEBUG:
    print('run complete, pushed/updated {} jobs.'.format(counter))
    logging.info('run complete, pushed/updated {} jobs.'.format(counter))

else:
    print('DEBUG run complete, updated 0 jobs.')
    logging.info('DEBUG run complete, updated 0 jobs.')
