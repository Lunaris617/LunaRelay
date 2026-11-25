"""Build a constrained Mihomo configuration from administrator subscriptions."""
import base64
import hashlib
import ipaddress
import os
from pathlib import Path
import socket
from urllib.parse import urlsplit

import requests
import yaml


def public_https(url, *, resolve=True):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.fragment:
        raise ValueError('请输入有效的 HTTPS 地址')
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if (address and not address.is_global) or parsed.hostname.lower().rstrip('.') in ('localhost',):
        raise ValueError('该地址必须使用公网主机')
    if resolve:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError('该地址必须解析到公网')
    return parsed


def direct():
    client = requests.Session()
    client.trust_env = False
    return client


def download(url, client=None):
    public_https(url, resolve=client is None)
    with (client or direct()) as transport, transport.get(
        url, timeout=(12, 20), stream=True, allow_redirects=False,
        headers={'User-Agent': 'Clash.Meta'},
    ) as response:
        if response.status_code != 200:
            raise ValueError(f'订阅服务器返回 HTTP {response.status_code}')
        chunks = bytearray()
        for chunk in response.iter_content(16384):
            chunks.extend(chunk)
            if len(chunks) > 1024 * 1024:
                raise ValueError('订阅文件超过 1 MB')
        return chunks.decode('utf-8-sig')


def subscription(text):
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError:
        parsed = None
    if isinstance(parsed, dict):
        candidates = parsed.get('proxies')
        if not isinstance(candidates, list):
            raise ValueError('订阅中没有 proxies 列表')
    else:
        if '://' not in text:
            encoded = ''.join(text.split())
            try:
                text = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True).decode()
            except (ValueError, UnicodeError):
                raise ValueError('无法识别订阅格式') from None
        candidates = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith('#')]
    if not 1 <= len(candidates) <= 500:
        raise ValueError('订阅应包含 1 至 500 个节点')
    return candidates


class Core:
    def __init__(self, directory, secret):
        self.path = Path(directory)
        self.secret = secret
        self.api = os.getenv('MIHOMO_API', 'http://mihomo:9090')
        self.address = os.getenv('MIHOMO_HOST', 'mihomo')

    def configure(self, candidates, exits):
        self.path.mkdir(parents=True, exist_ok=True)
        providers = {}
        udp = []
        check = {'url': 'https://www.gstatic.com/generate_204', 'interval': 60,
                 'timeout': 5000, 'lazy': False, 'expected-status': '204'}
        for node in candidates:
            if isinstance(node, dict):
                name = str(node.get('name', '')).strip()
                if not name or not node.get('server') or not node.get('port'):
                    continue
                node = {k: v for k, v in node.items() if k not in ('dialer-proxy', 'interface-name', 'routing-mark')}
                kind = node.get('type', '').lower()
                if kind in ('direct', 'reject', 'dns', 'relay'):
                    continue
                identity = name
                content = yaml.safe_dump({'proxies': [node]}, allow_unicode=True)
            elif isinstance(node, str) and node.split('://', 1)[0] in {
                'ss', 'ssr', 'vmess', 'vless', 'trojan', 'hysteria', 'hysteria2', 'hy2', 'tuic', 'anytls'
            }:
                import json
                from urllib.parse import unquote
                kind = node.split('://', 1)[0]
                # v2rayN embeds the human-readable VMess label in its JSON payload.
                name = unquote(urlsplit(node).fragment)
                if not name and node.startswith('vmess://'):
                    raw = node[8:]
                    try:
                        name = str(json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4))).get('ps', ''))
                    except (ValueError, UnicodeError):
                        continue
                identity = name or hashlib.sha256(node.encode()).hexdigest()[:12]
                content = node + '\n'
            else:
                continue
            key = 'node-' + hashlib.sha256(identity.encode()).hexdigest()[:16]
            if key in providers:
                continue
            filename = 'provider-' + hashlib.sha256(content.encode()).hexdigest()[:24] + '.txt'
            self.write(filename, content)
            providers[key] = {'type': 'file', 'path': './' + filename, 'override': {'additional-prefix': key + ' '},
                              'health-check': {'enable': True, **check}}
            if kind in ('hysteria2', 'hy2', 'hysteria', 'tuic'):
                udp.append(key)
        if not providers:
            raise ValueError('订阅没有可识别的节点')
        groups = []
        listeners = []
        if any(not selected for selected in exits.values()):
            other = [key for key in providers if key not in udp]
            auto_names = []
            for label, keys in (('udp', udp), ('other', other)):
                if keys:
                    name = f'auto-{label}'
                    groups.append({'name': name, 'type': 'url-test', 'use': keys, 'tolerance': 0, **check})
                    auto_names.append(name)
        for port_text, selected in exits.items():
            port = int(port_text)
            keys = [key for key in selected if key in providers] if selected else list(providers)
            name = f'exit-{port}'
            group = {'name': name, 'type': 'fallback', **check}
            if not selected:
                group['proxies'] = auto_names
            else:
                group.update({'use': keys} if keys else {'proxies': ['REJECT']})
            groups.append(group)
            listeners.append({'name': name, 'type': 'http', 'port': port, 'listen': '0.0.0.0', 'proxy': name})
        config = {
            'mode': 'rule', 'log-level': 'silent', 'external-controller': '0.0.0.0:9090',
            'secret': self.secret, 'proxy-providers': providers, 'proxy-groups': groups,
            'listeners': listeners, 'rules': ['MATCH,REJECT'],
        }
        self.write('candidate.yaml', yaml.safe_dump(config, allow_unicode=True))
        with direct() as client:
            response = client.put(self.api + '/configs?force=true', json={'path': '/mihomo-state/candidate.yaml'},
                                  headers={'Authorization': 'Bearer ' + self.secret}, timeout=15)
        if response.status_code >= 300:
            raise ValueError('Mihomo 校验失败，请检查订阅节点格式')
        os.replace(self.path / 'candidate.yaml', self.path / 'config.yaml')
        retained = {item['path'][2:] for item in providers.values()}
        for cached in self.path.glob('provider-*.txt'):
            if cached.name not in retained:
                cached.unlink(missing_ok=True)
        return list(providers)

    def status(self):
        with direct() as client:
            response = client.get(self.api + '/proxies', headers={'Authorization': 'Bearer ' + self.secret}, timeout=4)
            response.raise_for_status()
            return response.json().get('proxies', {})

    def client(self, port):
        client = direct()
        proxy = f'http://{self.address}:{int(port)}'
        client.proxies.update(http=proxy, https=proxy)
        return client

    def write(self, name, content):
        destination = self.path / name
        temporary = destination.with_suffix('.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(content)
        os.replace(temporary, destination)
