import docker
import json
import os
import os.path
import time
import urllib
import urllib2
import uuid
import yaml
from docker_compose import get_project, ps_
from flask import Flask, jsonify, request, abort
from gevent import pywsgi
from threading import Timer

# global vars
app = Flask(__name__)
docker_client = docker.from_env()
allow_origin = os.environ.get('MINIENV_ALLOW_ORIGIN')
node_host_name = os.environ.get('MINIENV_NODE_HOST_NAME')
provision_volume_driver = os.environ.get('MINIENV_PROVISION_VOLUME_DRIVER', '')
provision_volume_driver_opts = os.environ.get('MINIENV_PROVISION_VOLUME_DRIVER_OPTS', '')
provision_images = os.environ.get('MINIENV_PROVISION_IMAGES', '')
repo_whitelist = os.environ.get('MINIENV_REPO_WHITELIST', '')
environments = []

MINIENV_VERSION = "latest"

STATUS_IDLE = 0
STATUS_PROVISIONING = 1
STATUS_CLAIMED = 2
STATUS_RUNNING = 3
STATUS_UPDATING = 4

CHECK_ENV_TIMER_SECONDS = 15
DELETE_ENV_NO_ACIVITY_SECONDS = 60
EXPIRE_CLAIM_NO_ACIVITY_SECONDS = 30

VAR_MINIENV_VERSION = "$minienvVersion"
VAR_INTERNAL_LOG_PORT = "$internalLogPort"
VAR_INTERNAL_EDITOR_PORT = "$internalEditorPort"
VAR_INTERNAL_PROXY_PORT = "$internalProxyPort"
VAR_EXTERNAL_LOG_PORT = "$externalLogPort"
VAR_EXTERNAL_EDITOR_PORT = "$externalEditorPort"
VAR_EXTERNAL_PROXY_PORT = "$externalProxyPort"
VAR_GIT_REPO = "$gitRepo"
VAR_ALLOW_ORIGIN = "$allowOrigin"
VAR_VOLUME_NAME = "$volumeName"
VAR_PROVISON_IMAGES = "$provisionImages"


DEFAULT_INTERNAL_LOG_PORT = "30081"
DEFAULT_INTERNAL_EDITOR_PORT = "30082"
DEFAULT_INTERNAL_PROXY_PORT = "30083"
EXTERNAL_LOG_PORT_START = 40000
EXTERNAL_EDITOR_PORT_START = 40001
EXTERNAL_PROXY_PORT_START = 40002
EXTERNAL_PORT_INCREMENT = 10


@app.after_request
def add_header(r):
    r.headers['Access-Control-Allow-Origin'] = allow_origin
    r.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    r.headers['Cache-Control'] = 'no-store, must-revalidate'
    r.headers['Expires'] = '0'
    return r


@app.route('/api/whitelist', methods=['GET'])
def whitelist():
    return jsonify({'repos': []})


@app.route('/api/claim', methods=['POST'])
def claim():
    # if body is None throw error
    claim_request = request.get_json()
    if claim_request is None:
        abort(400)
        return
    claim_response = {}
    environment = None
    for element in environments:
        if element['status'] == STATUS_IDLE:
            environment = element
            break
    if environment is None:
        print('Claim failed; no environments available.')
        claim_response['claimGranted'] = False
        claim_response['message'] = 'No environments available'
    else:
        print('Claimed environment {}.'.format(environment['id']))
        claim_response['claimGranted'] = True
        claim_response['claimToken'] = str(uuid.uuid4())
        environment['claimToken'] = claim_response['claimToken']
        environment['status'] = STATUS_CLAIMED
        environment['lastActivity'] = time.time()
    return jsonify(claim_response)


@app.route('/api/ping', methods=['POST'])
def ping():
    # if body is None throw error
    ping_request = request.get_json()
    if ping_request is None:
        abort(400)
        return
    ping_response = {}
    environment = None
    for element in environments:
        if element['claimToken'] == ping_request['claimToken']:
            environment = element
            break
    if environment is None:
        ping_response['claimGranted'] = False
        ping_response['up'] = False
    else:
        environment['lastActivity'] = time.time()
        ping_response['claimGranted'] = True
        ping_response['up'] = environment['status'] == STATUS_RUNNING
        ping_response['repo'] = environment['repo']
        if ping_response['up'] and 'getEnvDetails' in ping_request.keys() and ping_request['getEnvDetails']:
            # make sure to check if it is really running
            exists = is_env_deployed(environment['id'])
            ping_response['up'] = exists
            if exists:
                ping_response['envDetails'] = environment['details']
            else:
                environment['status'] = STATUS_CLAIMED
                environment['repo'] = None
                environment['details'] = None
    return jsonify(ping_response)


@app.route('/api/up', methods=['POST'])
def up():
    up_request = request.get_json()
    if up_request is None:
        abort(400)
        return
    environment = None
    for element in environments:
        if element['claimToken'] == up_request['claimToken']:
            environment = element
            break
    if environment is None:
        print('Up request failed; claim no longer valid.')
        abort(401)
        return
    else:
        up_response = None
        # download minienv.json file
        print('Checking if deployment exists for env {}...'.format(environment['id']))
        if is_env_deployed(environment['id']):
            print('Env deployed for claim {}.'.format(environment['id']))
            if environment['status'] == STATUS_RUNNING and up_request['repo'] == environment['repo']:
                print('Returning existing environment details...')
                up_response = environment['details']
        if up_response is None:
            print('Creating new deployment...')
            # change status to updating, so the scheduler doesn't think it has stopped when the old repo is shutdown
            environment['status'] = STATUS_UPDATING
            details = deploy_env(up_request, environment)
            up_response = {
                'repo': up_request['repo'],
                'deployToBluemix': False,
                'logUrl': details['logUrl'],
                'editorUrl': details['editorUrl'],
                'tabs': details['tabs']
            }
            environment['status'] = STATUS_RUNNING
            environment['repo'] = up_request['repo']
            environment['details'] = up_response
        return jsonify(up_response)


def deploy_provisioner(environment):
    # create volume if it doesn't exist
    volume_name = get_volume_name(environment['id'])
    try:
        docker_client.volumes.get(volume_name)
    except docker.errors.NotFound:
        print('Creating volume \'{}\'...'.format(volume_name))
        kwargs = {}
        if len(provision_volume_driver) > 0:
            kwargs['driver'] = provision_volume_driver
        if len(provision_volume_driver_opts) > 0:
            driver_opts = {}
            driver_opts_list = provision_volume_driver_opts.split(",")
            if len(driver_opts_list) > 0:
                for driver_opt in driver_opts_list:
                    driver_opt_parts = driver_opt.split(':')
                    if len(driver_opt_parts) == 2:
                        driver_opts[driver_opt_parts[0]] = driver_opt_parts[1]
            kwargs['driver_opts'] = driver_opts
        docker_client.volumes.create(volume_name, **kwargs)
    # check if environment already running
    if is_provisioner_running(environment['id']):
        print('Deleting existing provisioner {}...'.format(environment['id']))
        delete_provisioner(environment['id'])
    # run using docker-compose
    project_name = get_provisioner_project_name(environment['id'])
    src_file_name = './docker-compose-provision.yml.template'
    dest_file_name = './docker-compose-{}.yml'.format(project_name)
    src_file = open(src_file_name, 'r')
    dest_file = open(dest_file_name, 'w')
    for line in src_file:
        line = line.replace(VAR_MINIENV_VERSION, MINIENV_VERSION)
        line = line.replace(VAR_PROVISON_IMAGES, provision_images)
        line = line.replace(VAR_VOLUME_NAME, volume_name)
        dest_file.write(line)
    src_file.close()
    dest_file.close()
    project = get_project('./', project_name, dest_file_name)
    project.up(detached=True, strategy=2)  # strategy 2 = always re-create


def is_provisioner_running(env_id):
    project_name = get_provisioner_project_name(env_id)
    project_file_name = './docker-compose-{}.yml'.format(project_name)
    if os.path.isfile(project_file_name):
        project = get_project('./', project_name, project_file_name)
        ps = ps_(project)
        return is_project_starting(ps) or is_project_running(ps)
    else:
        return False


def delete_provisioner(env_id):
    project_name = get_provisioner_project_name(env_id)
    project_file_name = './docker-compose-{}.yml'.format(project_name)
    project = get_project('./', project_name, project_file_name)
    ps = ps_(project)
    if is_project_running(ps):
        project.down(1, False, remove_orphans=True)
    os.remove(project_file_name)
    
    
def deploy_env(up_request, environment):
    print('Deploying environment {}...'.format(environment['id']))
    minienv_dict = {}
    minienv_json = None
    try:
        response = urllib2.urlopen('{}/raw/master/minienv.json'.format(up_request['repo']))
        minienv_json = response.read()
    except:
        print('Error downloading minienv.json')
    if minienv_json is not None and len(minienv_json) > 0:
        minienv_dict = json.loads(minienv_json)
    # download docker-compose file (first try yml, then yaml)
    docker_compose_yaml = None
    try:
        response = urllib2.urlopen('{}/raw/master/docker-compose.yml'.format(up_request['repo']))
        docker_compose_yaml = response.read()
    except:
        print('Error downloading docker-compose.yml')
    if docker_compose_yaml is None or len(docker_compose_yaml) == 0:
        try:
            response = urllib2.urlopen('{}/raw/master/docker-compose.yaml'.format(up_request['repo']))
            docker_compose_yaml = response.read()
        except:
            print('Error downloading docker-compose.yaml')
    if docker_compose_yaml is None or len(docker_compose_yaml) == 0:
        abort(400)
        return
    docker_compose_dict = yaml.safe_load(docker_compose_yaml)
    # check if environment already running
    if is_env_deployed(environment['id']):
        print('Deleting existing environment {}...'.format(environment['id']))
        delete_env(environment['id'])
    # run using docker-compose
    project_name = get_env_project_name(environment['id'])
    volume_name = get_volume_name(environment['id'])
    src_file_name = './docker-compose-env.yml.template'
    dest_file_name = './docker-compose-{}.yml'.format(project_name)
    src_file = open(src_file_name, 'r')
    dest_file = open(dest_file_name, 'w')
    external_log_port = str(EXTERNAL_LOG_PORT_START+(environment['index']*EXTERNAL_PORT_INCREMENT))
    external_editor_port = str(EXTERNAL_EDITOR_PORT_START+(environment['index']*EXTERNAL_PORT_INCREMENT))
    external_proxy_port = str(EXTERNAL_PROXY_PORT_START+(environment['index']*EXTERNAL_PORT_INCREMENT))
    for line in src_file:
        line = line.replace(VAR_INTERNAL_LOG_PORT, DEFAULT_INTERNAL_LOG_PORT)
        line = line.replace(VAR_INTERNAL_EDITOR_PORT, DEFAULT_INTERNAL_EDITOR_PORT)
        line = line.replace(VAR_INTERNAL_PROXY_PORT, DEFAULT_INTERNAL_PROXY_PORT)
        line = line.replace(VAR_EXTERNAL_LOG_PORT, external_log_port)
        line = line.replace(VAR_EXTERNAL_EDITOR_PORT, external_editor_port)
        line = line.replace(VAR_EXTERNAL_PROXY_PORT, external_proxy_port)
        line = line.replace(VAR_GIT_REPO, up_request['repo'])
        line = line.replace(VAR_ALLOW_ORIGIN, allow_origin)
        line = line.replace(VAR_VOLUME_NAME, volume_name)
        dest_file.write(line)
    src_file.close()
    dest_file.close()
    project = get_project('./', project_name, dest_file_name)
    print('Running docker-compose up for environment {}...'.format(environment['id']))
    project.up(detached=True, strategy=2)  # strategy 2 = always re-create
    ps = ps_(project)
    return get_up_details(ps, docker_compose_dict, minienv_dict)


def is_env_deployed(env_id):
    project_name = get_env_project_name(env_id)
    project_file_name = './docker-compose-{}.yml'.format(project_name)
    if os.path.isfile(project_file_name):
        project = get_project('./', project_name, project_file_name)
        ps = ps_(project)
        return is_project_starting(ps) or is_project_running(ps)
    else:
        return False


def delete_env(env_id):
    project_name = get_env_project_name(env_id)
    project_file_name = './docker-compose-{}.yml'.format(project_name)
    project = get_project('./', project_name, project_file_name)
    ps = ps_(project)
    if is_project_running(ps):
        project.down(1, True, remove_orphans=True)
    wait_time = 0
    while is_project_running(ps) and wait_time < 120:
        print('Waiting for environment {} deletion...'.format(env_id))
        wait_time = wait_time + 15
        time.sleep(15)
        ps = ps_(project)
    os.remove(project_file_name)


def get_up_details(ps, docker_compose_dict, minienv_dict):
    details = {'node_host_name': node_host_name}
    tabs = []
    if len(ps) > 0 and 'ports' in ps[0].keys():
        ports = ps[0]['ports']
        for key in ports.keys():
            port_str = key[0:key.index('/')]
            if ports[key] is not None and len(ports[key]) > 0:
                host_port_str = ports[key][0]['HostPort']
                host_port = int(host_port_str)
                if port_str == DEFAULT_INTERNAL_LOG_PORT:
                    details['logPort'] = host_port
                    details['logUrl'] = 'http://{}:{}'.format(node_host_name, host_port)
                elif port_str == DEFAULT_INTERNAL_EDITOR_PORT:
                    details['editorPort'] = host_port
                    details['editorUrl'] = 'http://{}:{}'.format(node_host_name, host_port)
                    if 'editor' in minienv_dict.keys():
                        editor = minienv_dict['editor']
                        if 'hide' in editor.keys() and editor['hide']:
                            details['editorPort'] = 0
                            details['editorUrl'] = ''
                        elif 'srcDir' in editor.keys() and len(editor['srcDir']) > 0:
                            details['editorUrl'] = '{}?src={}'.format(details['editorUrl'], urllib.quote(editor['srcDir']))
                elif port_str == DEFAULT_INTERNAL_PROXY_PORT:
                    details['proxyPort'] = host_port
    proxy_ports = []
    if 'proxy' in minienv_dict.keys():
        if 'ports' in minienv_dict['proxy'].keys():
            proxy_ports = minienv_dict['proxy']['ports']
    if 'services' in docker_compose_dict.keys():
        services = docker_compose_dict['services']
        for key in services.keys():
            if 'ports' in services[key].keys():
                ports = services[key]['ports']
                if ports is not None and len(ports) > 0:
                    for port_str in ports:
                        tab_port_str = port_str[0:port_str.index(':')]
                        tab_port = int(tab_port_str)
                        tab = {'port': tab_port, 'name': tab_port_str, 'path': ''}
                        extra_tabs = []
                        hide_tab = False
                        for proxy_port in proxy_ports:
                            if 'port' in proxy_port.keys() and proxy_port['port'] == tab_port:
                                if 'hide' in proxy_port.keys() and proxy_port['hide']:
                                    hide_tab = True
                                    break
                                if 'tabs' in proxy_port.keys() and len(proxy_port['tabs']) > 0:
                                    for i, proxy_tab in enumerate(proxy_port['tabs']):
                                        if i == 0:
                                            if 'name' in proxy_tab.keys():
                                                tab['name'] = proxy_tab['name']
                                            if 'path' in proxy_port.keys():
                                                tab['path'] = proxy_port['path']
                                        else:
                                            extra_tab = {'port': tab_port, 'name': tab_port_str, 'path': ''}
                                            if 'name' in proxy_tab.keys():
                                                extra_tab['name'] = proxy_tab['name']
                                            if 'path' in proxy_port.keys():
                                                extra_tab['path'] = proxy_port['path']
                                            extra_tabs.append(extra_tab)
                                else:
                                    if 'name' in proxy_port.keys():
                                        tab['name'] = proxy_port['name']
                                    if 'path' in proxy_port.keys():
                                        tab['path'] = proxy_port['path']
                        if not hide_tab:
                            tabs.append(tab)
                            if len(extra_tabs) > 0:
                                tabs.extend(extra_tabs)
    for tab in tabs:
        tab['url'] = 'http://{}.{}:{}{}'.format(tab['port'], node_host_name, details['proxyPort'], tab['path'])
    details['tabs'] = tabs
    return details


def is_project_starting(ps):
    # TODO:necessary?
    if len(ps) == 0 or 'is_running' not in ps[0].keys():
        return False
    else:
        return ps[0]['is_running']


def is_project_running(ps):
    if len(ps) == 0 or 'is_running' not in ps[0].keys():
        return False
    else:
        return ps[0]['is_running']


def get_provisioner_project_name(env_id):
    return 'minienv-env-{}-provision'.format(env_id.lower())


def get_env_project_name(env_id):
    return 'minienv-env-{}'.format(env_id.lower())


def get_volume_name(env_id):
    return 'minienv-env-{}-volume'.format(env_id.lower())


def init_environments(env_count):
    print('Provisioning {} environments...'.format(env_count))
    for i in range(0, env_count):
        environment = {
            'id': str(i + 1),
            'index': i,
            'status': STATUS_IDLE,
            'claimToken': '',
            'lastActivity': 0,
            'repo': None,
            'upResponse': None,
        }
        environments.append(environment)
        # check if environment running
        running = False
        if is_env_deployed(environment['id']):
            print('Loading running environment {}...'.format(environment['id']))
            environment['status'] = STATUS_RUNNING
            # TODO: environment.ClaimToken =
            environment['lastActivity'] = time.time()
            # TODO: environment.UpRequest = ???
            # TODO: environment.UpResponse = ???
            # running = True
        if not running:
            print('Provisioning environment {}...'.format(environment['id']))
            environment['status'] = STATUS_PROVISIONING
            deploy_provisioner(environment)
            wait_time = 0
            while is_provisioner_running(environment['id']) and wait_time < 120:
                wait_time = wait_time + 15
                time.sleep(15)
    start_environment_check_timer()


def start_environment_check_timer():
    t = Timer(CHECK_ENV_TIMER_SECONDS, check_environments)
    t.start()


def check_environments():
    for environment in environments:
        print('Checking environment {}; current status={}'.format(environment['id'], environment['status']))
        if environment['status'] == STATUS_PROVISIONING:
            # TODO: not supported yet
            if not is_provisioner_running(environment['id']):
                print('Environment {} provisioning complete.'.format(environment['id']))
                environment['status'] = STATUS_IDLE
                delete_provisioner(environment['id'])
            else:
                print('Environment {} still provisioning...'.format(environment['id']))
        elif environment['status'] == STATUS_RUNNING:
            if time.time() - environment['lastActivity'] > DELETE_ENV_NO_ACIVITY_SECONDS:
                print('Environment {} no longer active.'.format(environment['id']))
                environment['status'] = STATUS_IDLE
                environment['claimToken'] = ''
                environment['lastActivity'] = 0
                environment['upRequest'] = None
                environment['details'] = None
                (environment['id'])
            else:
                print('Checking if environment {} is still deployed...'.format(environment['id']))
                if not is_env_deployed(environment['id']):
                    print('Environment {} no longer deployed.'.format(environment['id']))
                    environment['status'] = STATUS_IDLE
                    environment['claimToken'] = ''
                    environment['lastActivity'] = 0
                    environment['upRequest'] = None
                    environment['details'] = None
        elif environment['status'] == STATUS_CLAIMED:
            if time.time() - environment['lastActivity'] > EXPIRE_CLAIM_NO_ACIVITY_SECONDS:
                print('Environment {} claim expired.'.format(environment['id']))
                environment['status'] = STATUS_IDLE
                environment['claimToken'] = ''
                environment['lastActivity'] = 0
                environment['upRequest'] = None
                environment['details'] = None
    start_environment_check_timer()

if __name__ == '__main__':
    try:
        port = int(os.getenv('PORT', 8080))
        env_count = int(os.getenv('MINIENV_PROVISION_COUNT', 1))
        init_environments(env_count)
        server = pywsgi.WSGIServer(('', port), app)
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
