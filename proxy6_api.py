import os
import requests
from datetime import datetime

API_KEY = os.getenv('PROXY6_API_KEY')
BASE_URL = 'https://proxy6.net/api'

def buy_vpn(period_days=30, country='ru', version='6'):
    """
    Покупает 1 VPN-ключ (OpenVPN) на указанный срок.
    Возвращает словарь с данными ключа.
    """
    resp = requests.get(f'{BASE_URL}/{API_KEY}/buy', params={
        'count': 1,
        'period': period_days,
        'country': country,
        'version': version,
        'type': 'vpn',
        'format': 'json'
    }, timeout=15)
    data = resp.json()
    if data.get('status') != 'yes':
        error = data.get('error', 'Неизвестная ошибка')
        raise Exception(f'Ошибка покупки VPN: {error}')
    
    item = data['list'][list(data['list'].keys())[0]]
    return {
        'id': item['id'],
        'config_url': item['config'],          # прямая ссылка на .ovpn
        'expire_date': datetime.fromtimestamp(int(item['expire_date'])),
        'ip': item['ip'],
        'login': item['login'],
        'password': item['pass'],
        'country': item['country']
    }

def prolong_vpn(vpn_id, period_days=30):
    """Продлевает существующий VPN-ключ."""
    resp = requests.get(f'{BASE_URL}/{API_KEY}/prolong', params={
        'id': vpn_id,
        'period': period_days
    }, timeout=15)
    data = resp.json()
    return data.get('status') == 'yes'

def get_vpn_info(vpn_id):
    """Получить информацию о конкретном ключе (срок, статус)."""
    resp = requests.get(f'{BASE_URL}/{API_KEY}/getinfo', timeout=15)
    data = resp.json()
    if data.get('status') == 'yes':
        return data['list'].get(str(vpn_id))
    return None

def get_balance():
    """Текущий баланс в рублях."""
    resp = requests.get(f'{BASE_URL}/{API_KEY}/getcount', timeout=15)
    data = resp.json()
    if data.get('status') == 'yes':
        return float(data['balance'])
    return 0.0