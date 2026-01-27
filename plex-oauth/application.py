"""
AWS Elastic Beanstalk entry point
EB looks for 'application' variable by default
"""

from app import app as application

if __name__ == "__main__":
    application.run()
