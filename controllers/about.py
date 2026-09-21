from flask import Blueprint, render_template

# Define the blueprint for about routes
about_bp = Blueprint('about', __name__)

@about_bp.route('/about')
def about():
    return render_template('about.html')

@about_bp.route('/about/licence')
def licence_page():
    return render_template('licence.html')

@about_bp.route('/about/tos')
def tos():
    return render_template('tos.html')

@about_bp.route('/about/privacy')
def privacy():
    return render_template('privacy.html')
