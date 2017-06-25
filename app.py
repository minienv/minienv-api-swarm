import os
from flask import Flask, jsonify
from gevent import pywsgi

# global vars
app = Flask(__name__)


@app.after_request
def add_header(r):
    r.headers['Access-Control-Allow-Origin'] = os.environ.get('EXUP_ALLOW_ORIGIN')
    r.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    r.headers['Cache-Control'] = 'no-store, must-revalidate'
    r.headers['Expires'] = '0'
    return r


@app.route('/api/up', methods=['POST'])
def up():
    return jsonify(up=False)


@app.route('/api/ping', methods=['POST'])
def ping():
    return jsonify(up=False)


if __name__ == '__main__':
    try:
        port = int(os.getenv('PORT', 8080))
        server = pywsgi.WSGIServer(('', port), app)
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass