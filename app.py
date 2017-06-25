import os
from docker_compose import get_project, ps_
from flask import Flask, jsonify, request, abort
from gevent import pywsgi

# global vars
app = Flask(__name__)
allowOrigin = os.environ.get('EXUP_ALLOW_ORIGIN')
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
        return jsonify(up_response)
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
    # stop project
    project.down(2, True, remove_orphans=True)
    # start project
    project.up()
    # TODO:get ports
    print(ps_(project))
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