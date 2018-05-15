# ad_policy
Install:

`git clone https://github.com/lorist/ad_policy.git .`

`cd ad_policy && virtualenv venv`

`source venv/bin/activate`

`pip install -r requirements.txt`

Edit the py and add your AD details

# ad_policy service

sudo cp ~/ad_policy/ad_policy.service /etc/systemd/system/

# nginx

`sudo cp ~/ad_policy/ad_policy.nginx /etc/nginx/sites-available/`

`sudo ln -s /etc/nginx/sites-available/ad_policy.nginx /etc/nginx/sites-enabled`




