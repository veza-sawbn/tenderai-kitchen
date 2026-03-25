# app.py

from flask import Flask, render_template, request
from yourapp import db

app = Flask(__name__)

# Convert ORM objects to dictionaries

def object_to_dict(obj):
    return {"id": obj.id, "name": obj.name, "description": obj.description}

@app.context_processor

def inject_globals():
    items = db.session.query(Item).all()  # Replace with your ORM model
    return {'items': [object_to_dict(item) for item in items]}

@app.route('/')
def home():
    return render_template('home.html')

@app.route('/about')
def about():
    return render_template('about.html')

# Other routes and functions remain unchanged...

if __name__ == '__main__':
    app.run(debug=True)