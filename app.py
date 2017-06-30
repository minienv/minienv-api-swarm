import json
import os
import urllib
import urllib2
import uuid
import yaml
from docker_compose import get_project, ps_
from flask import Flask, jsonify, request, abort
from gevent import pywsgi

# global vars
app = Flask(__name__)
allowOrigin = os.environ.get('MINIENV_ALLOW_ORIGIN')
nodeHostName = os.environ.get('MINIENV_NODE_HOST_NAME')
environments = {}
max_environments = 2

VAR_LOG_PORT = "$logPort"
VAR_EDITOR_PORT = "$editorPort"
VAR_PROXY_PORT = "$proxyPort"
VAR_GIT_REPO = "$gitRepo"
VAR_ALLOW_ORIGIN = "$allowOrigin"
VAR_VOLUME_NAME = "$volumeName"

DEFAULT_LOG_PORT = "30081"
DEFAULT_EDITOR_PORT = "30082"
DEFAULT_PROXY_PORT = "30083"


@app.after_request
def add_header(r):
    r.headers['Access-Control-Allow-Origin'] = allowOrigin
    r.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    r.headers['Cache-Control'] = 'no-store, must-revalidate'
    r.headers['Expires'] = '0'
    return r


@app.route('/api/claim', methods=['POST'])
def claim():
    # if body is None throw error
    claim_response = {}
    claim_request = request.get_json()
    if claim_request is None:
        abort(400)
        return
    if len(environments) >= max_environments:
        # max environment exceeded - do not grant claim
        claim_response['claimGranted'] = False
        claim_response['message'] = 'No more claims available'
    else:
        claim_response['claimGranted'] = True
        claim_response['claimToken'] = str(uuid.uuid4())
        claim_response['claimId'] = str(len(environments) + 1)
        environment = {'claimId': claim_response['claimId'], 'claimToken': claim_response['claimToken']}
        environments[claim_response['claimToken']] = environment
    return jsonify(claim_response)


@app.route('/api/ping', methods=['POST'])
def ping():
    # if body is None throw error
    ping_response = {}
    ping_request = request.get_json()
    if ping_request is None:
        abort(400)
        return jsonify(ping_response)
    if 'claimToken' in ping_request.keys() and ping_request['claimToken'] in environments.keys():
        environment = environments[ping_request['claimToken']]
        ping_response['claimGranted'] = True
        ping_response['up'] = 'upRequest' in environment.keys() and 'upResponse' in environment.keys()
        if ping_response['up'] and 'getUpDetails' in ping_request.keys() and ping_request['getUpDetails']:
            # make sure to check if it is really running
            exists = is_example_deployed(environment['claimId'])
            if exists:
                ping_response['upDetails'] = environment['upResponse']
            else:
                environment.pop('upRequest', None)
                environment.pop('upResponse', None)
    else:
        ping_response['claimGranted'] = False
        ping_response['up'] = False
    return jsonify(ping_response)


@app.route('/api/up', methods=['POST'])
def up():
    up_request = request.get_json()
    up_response = {'up': False}
    if up_request is None:
        abort(400)
        return
    if up_request['claimToken'] not in environments.keys():
        print("Up request failed; claim no longer valid.")
        abort(401)
        return
    environment = environments[up_request['claimToken']]
    # download minienv.json file
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
    # run using docker-compose
    project_name = get_project_name(environment['claimId'])
    volume_name = get_volume_name(environment['claimId'])
    src_file_name = './docker-compose.yml.template'
    dest_file_name = './docker-compose-{}.yml'.format(project_name)
    src_file = open(src_file_name, 'r')
    dest_file = open(dest_file_name, 'w')
    for line in src_file:
        line = line.replace(VAR_LOG_PORT, DEFAULT_LOG_PORT)
        line = line.replace(VAR_EDITOR_PORT, DEFAULT_EDITOR_PORT)
        line = line.replace(VAR_PROXY_PORT, DEFAULT_PROXY_PORT)
        line = line.replace(VAR_GIT_REPO, up_request['repo'])
        line = line.replace(VAR_ALLOW_ORIGIN, allowOrigin)
        line = line.replace(VAR_VOLUME_NAME, volume_name)
        dest_file.write(line)
    src_file.close()
    dest_file.close()
    project = get_project('./', project_name, dest_file_name)
    ps = ps_(project)
    if is_project_running(ps):
        project.down(2, True, remove_orphans=True)
    project.up()
    ps = ps_(project)
    details = get_up_details(ps, docker_compose_dict, minienv_dict)
    up_response['repo'] = up_request['repo']
    up_response['deployToBluemix'] = False # TODO:
    up_response['logUrl'] = details['logUrl']
    up_response['editorUrl'] = details['editorUrl']
    up_response['tabs'] = details['tabs']
    environment['upRequest'] = up_request
    environment['upResponse'] = up_response
    return jsonify(up_response)


def is_example_deployed(user_id):
    project_name = get_project_name(user_id)
    project_file_name = './docker-compose-{}.yml'.format(project_name)
    project = get_project('./', project_name, project_file_name)
    ps = ps_(project)
    return is_project_running(ps)


def get_up_details(ps, docker_compose_dict, minienv_dict):
    details = {'nodeHostName': nodeHostName}
    tabs = []
    if len(ps) > 0 and 'ports' in ps[0].keys():
        ports = ps[0]['ports']
        for key in ports.keys():
            port_str = key[0:key.index('/')]
            if ports[key] is not None and len(ports[key]) > 0:
                host_port_str = ports[key][0]['HostPort']
                host_port = int(host_port_str)
                if port_str == DEFAULT_LOG_PORT:
                    details['logPort'] = host_port
                    details['logUrl'] = 'http://{}:{}'.format(nodeHostName, host_port)
                elif port_str == DEFAULT_EDITOR_PORT:
                    details['editorPort'] = host_port
                    details['editorUrl'] = 'http://{}:{}'.format(nodeHostName, host_port)
                    if 'editor' in minienv_dict.keys():
                        editor = minienv_dict['editor']
                        if 'hide' in editor.keys() and editor['hide']:
                            details['editorPort'] = 0
                            details['editorUrl'] = ''
                        elif 'srcDir' in editor.keys() and len(editor['srcDir']) > 0:
                            details['editorUrl'] = '{}?src={}'.format(details['editorUrl'], urllib.quote(editor['srcDir']))
                elif port_str == DEFAULT_PROXY_PORT:
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
        tab['url'] = 'http://{}.{}:{}{}'.format(tab['port'], nodeHostName, details['proxyPort'], tab['path'])
    details['tabs'] = tabs
    return details


def is_project_running(ps):
    if len(ps) == 0 or 'is_running' not in ps[0].keys():
        return False
    else:
        return ps[0]['is_running']


def get_project_name(user_id):
    return 'u-{}'.format(user_id.lower())


def get_volume_name(user_id):
    return 'u-{}-volume'.format(user_id.lower())

if __name__ == '__main__':
    try:
        port = int(os.getenv('PORT', 8080))
        server = pywsgi.WSGIServer(('', port), app)
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
