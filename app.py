import json
import os
import urllib2
import yaml
from docker_compose import get_project, ps_
from flask import Flask, jsonify, request, abort
from gevent import pywsgi

# global vars
app = Flask(__name__)
allowOrigin = os.environ.get('EXUP_ALLOW_ORIGIN')
nodeHostName = os.environ.get('EXUP_NODE_HOST_NAME')
deployments = {}

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


@app.route('/api/up', methods=['POST'])
def up():
    up_request = request.get_json()
    up_response = {'up': False}
    if up_request is None:
        abort(400)
        return
    # download exampleup.json file
    response = urllib2.urlopen('{}/raw/master/exampleup.json'.format(up_request['repo']))
    exampleup_dict = {}
    exampleup_json = response.read()
    if exampleup_json is not None and len(exampleup_json) > 0:
        exampleup_dict = json.loads(exampleup_json)
    # download docker-compose file
    response = urllib2.urlopen('{}/raw/master/docker-compose.yml'.format(up_request['repo']))
    docker_compose_yaml = response.read()
    if docker_compose_yaml is None or len(docker_compose_yaml) == 0:
        abort(400)
        return
    docker_compose_dict = yaml.safe_load(docker_compose_yaml)
    # run using docker-compose
    project_name = get_project_name(up_request['userId'])
    volume_name = get_volume_name(up_request['userId'])
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
    details = get_up_details(ps, docker_compose_dict, exampleup_dict)
    up_response['repo'] = up_request['repo']
    up_response['deployToBluemix'] = False # TODO:
    up_response['logUrl'] = details['logUrl']
    up_response['editorUrl'] = details['editorUrl']
    up_response['tabs'] = details['tabs']
    print(json.dumps(up_response))
    return jsonify(up_response)


@app.route('/api/ping', methods=['POST'])
def ping():
    # if body is None throw error
    ping_response = {'up': False}
    ping_request = request.get_json()
    if ping_request is None:
        abort(400)
        return jsonify(ping_response)
    if 'userId' in ping_request.keys() and ping_request['userId'] in deployments.keys():
        ping_response['up'] = True
        if 'getUpDetails' in ping_request.keys() and ping_request['getUpDetails']:
            # TODO get details here
            pass
    return jsonify(ping_response)


def get_up_details(ps, docker_compose_dict, exampleup_dict):
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
                elif port_str == DEFAULT_PROXY_PORT:
                    details['proxyPort'] = host_port

    # if exampleupConfig.Editor != nil {
    # if exampleupConfig.Editor.Hide {
    # details.EditorPort = 0
    # details.EditorUrl = ""
    # } else if exampleupConfig.Editor.SrcDir != "" {
    # details.EditorUrl += "?src=" + url.QueryEscape(exampleupConfig.Editor.SrcDir)
    # }
    # }

    print(exampleup_dict)
    if 'services' in docker_compose_dict.keys():
        services = docker_compose_dict['services']
        for key in services.keys():
            if 'ports' in services[key].keys():
                ports = services[key]['ports']
                if ports is not None and len(ports) > 0:
                    for port_str in ports:
                        port = port_str[0:port_str.index(':')]
                        tabs.append({'port': int(port), 'name': port, 'path': ''})
    for tab in tabs:
        tab['url'] = 'http://{}.{}:{}{}'.format(tab['port'], nodeHostName, details['proxyPort'], tab['path'])
    details['tabs'] = tabs
    print(json.dumps(details))
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
