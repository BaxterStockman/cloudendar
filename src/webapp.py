#import gevent.monkey; gevent.monkey.patch_all()
import flask
import gevent.wsgi
import os
import pprint
import re
import time
import werkzeug.serving

from database import db_session, db_init
from flask.ext.bootstrap import Bootstrap
from flask.ext.moment import Moment
from flask.ext.socketio import SocketIO, emit
from forms import EventForm, LoginForm, SearchForm, SignupForm
from wtforms import SelectField
from wtforms.validators import InputRequired
from threading import Thread
from time import sleep
from whoosh import index
from whoosh.fields import Schema, TEXT
from whoosh.analysis import StemmingAnalyzer
from whoosh.qparser import QueryParser, MultifieldParser


schema = Schema(name=TEXT(stored=True), value=TEXT(stored=True))
if not os.path.exists('data'):
    os.mkdir('data')


ix = index.create_in('data', schema=schema, indexname="dummy_choices")
#if not index.exists_in('data', indexname="dummy_choices"):
#    ix = index.create_in('data', schema=schema, indexname="dummy_choices")
#else:
#    ix = index.open_dir('data', indexname="dummy_choices")


dummy_users = ['Alice', 'Bob', 'Carol', 'Del', 'Edith', 'Frank', 'Gertrude',
                'Hiram', 'Ilse', 'Jon', 'Karen', 'Lon', 'Matilda', 'Nick', 'Oprah', 'Pete',
                'Quinn', 'Rob', 'Sarah', 'Ted', 'Ursula', 'Von', 'Wendy', 'Xavier', 'Zelda']
dummy_choices = [(unicode(e), unicode(e)) for e in dummy_users]


def index_choices(ix, choices):
    with ix.writer() as writer:
        for name, value in choices:
            writer.add_document(name=name, value=value)


def search_index(ix, term):
    qp = MultifieldParser(['name', 'value'], schema=schema)
    q = qp.parse(term)
    with ix.searcher() as searcher:
        return searcher.search(q, limit=20)


def create_app():
    app = flask.Flask(__name__)
    app.config.update(
        SECRET_KEY='secret',
    )

    Bootstrap(app)
    Moment(app)
    index_choices(ix, dummy_choices)

    return app


app = create_app()
socketio = SocketIO(app)
db_init()


# Hooks
@app.teardown_appcontext
def shutdown_db_session(exception=None):
    db_session.remove()


@app.route('/')
def index():
    return flask.render_template('index.html')


@app.route('/user/<name>')
def user(name):
    return flask.render_template('user.html', name=name)


@app.route('/login', methods=["GET", "POST"])
def login_form():
    form = LoginForm()
    if form.validate_on_submit():
        flask.flash("Success!")
        flask.session['username'] = form.username.data
        return flask.redirect(flask.url_for('index'))
    return flask.render_template('login.html', form=form)


@app.route('/signup', methods=["GET", "POST"])
def signup_form():
    form = SignupForm()
    if form.validate_on_submit():
        flask.flash("Success!")
        flask.session['username'] = form.username.data
        return flask.redirect(flask.url_for('index'))
    return flask.render_template('signup.html', form=form)


@app.route('/add', methods=["GET", "POST"])
def event_form():
    form = EventForm()
    if form.validate_on_submit():
        flask.flash("Success!")
        print(form.start.data)
        print(form.end.data)
        return flask.redirect(flask.url_for('index'))
    return flask.render_template('search.html', form=form)


@app.route('/results')
def print_results():
    res = search_index(ix, "o")
    pprint.pprint(res)
    pprint.pprint(ix)
    for item in res:
        print(item)
    print(res)
    return "Hello, world!"


@app.route('/search', methods=["GET", "POST"])
def search_form():
    form = SearchForm()
    if form.validate_on_submit():
        flask.flash("Success!")
        print(form.start.data)
        print(form.end.data)
        return flask.redirect(flask.url_for('index'))
    return flask.render_template('search.html', form=form)


@socketio.on('search', namespace='/search')
def get_possibles(msg):
    onid = msg.get('data')
    print(onid)
    if onid is None:
        ret = "Empty email"
    else:
        match = re.match(r'(.+)@.+', onid)
        user = match.group(1)
        if user is None:
            ret = "Invalid email"
        hits = filter(lambda e: e == user, dummy_users)
        if len(hits) > 0:
            ret = "Found match: {0}".format(hits[0])
        else:
            ret = "No such user"
    emit('result', {'data': ret})


def search_user(num):
    label = "User %d" % num;
    return SelectField(label,
                       validators=[InputRequired()],
                       choices=dummy_choices,
                       )


@app.route('/expand')
def expand_form(users=1):
    class ExpandForm(SearchForm):
        pass
    for user in range(1, users + 1):
        setattr(ExpandForm, "user%d" % user, search_user(user))
    form = ExpandForm()
    if form.validate_on_submit():
        flask.flash("Success!")
        print(form.start.data)
        print(form.end.data)
        return flask.redirect(flask.url_for('index'))
    return flask.render_template('search.html', form=form)


@app.errorhandler(404)
def page_not_found(e):
    return flask.render_template('404.html'), 404


@app.errorhandler(500)
def internal_server_error(e):
    return flask.render_template('500.html'), 500


# This needs to be at the bottom of the file, just before 'if __name__ ...'
#@werkzeug.serving.run_with_reloader
def run_server():
    #app.debug = True
    #Thread(target=background_thread).start()
    #http_server = gevent.wsgi.WSGIServer(('', 5000), app)
    #http_server.serve_forever()
    return


if __name__ == "__main__":
    app.debug = True
    socketio.run(app)
