from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import os
from dotenv import load_dotenv
from flask_sqlalchemy import SQLAlchemy
import routeros_api
import re
from datetime import date, timedelta
from sqlalchemy import func
from dateutil.relativedelta import relativedelta
from datetime import date, datetime, timedelta

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///routers.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

class Router(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    ip = db.Column(db.String(50), nullable=False)
    username = db.Column(db.String(50), nullable=False)
    password = db.Column(db.String(100), nullable=False)

class LoadBalanceConfig(db.Model):
    id              = db.Column(db.Integer, primary_key=True)
    router_id       = db.Column(db.Integer, db.ForeignKey('router.id'), nullable=False)
    pcc_mode        = db.Column(db.String(20), nullable=False)   # 'both' | 'src' | 'dst'
    lan_network     = db.Column(db.String(50), nullable=False)
    failover_enabled = db.Column(db.Boolean, default=True)

    wans = db.relationship('WanEntry', backref='config', cascade='all, delete-orphan', order_by='WanEntry.priority')


class WanEntry(db.Model):
    id        = db.Column(db.Integer, primary_key=True)
    config_id = db.Column(db.Integer, db.ForeignKey('load_balance_config.id'), nullable=False)
    interface = db.Column(db.String(50), nullable=False)
    gateway   = db.Column(db.String(50), nullable=False)
    weight    = db.Column(db.Integer, default=1)
    priority  = db.Column(db.Integer, nullable=False)  # urutan failover, 1 = utama

class PPPoEProfile(db.Model):
    __tablename__ = 'pppoe_profile'
    id              = db.Column(db.Integer, primary_key=True)
    router_id       = db.Column(db.Integer, db.ForeignKey('router.id'), nullable=False)
    nama            = db.Column(db.String(100), nullable=False)
    rate_limit      = db.Column(db.String(50))
    harga_bulanan   = db.Column(db.Integer, nullable=False, default=0)
    local_address   = db.Column(db.String(50))
    remote_pool     = db.Column(db.String(50))


class PPPoECustomer(db.Model):
    __tablename__ = 'pppoe_customer'
    id                  = db.Column(db.Integer, primary_key=True)
    router_id           = db.Column(db.Integer, db.ForeignKey('router.id'), nullable=False)
    nama_pelanggan      = db.Column(db.String(100), nullable=False)
    no_hp               = db.Column(db.String(30))
    alamat              = db.Column(db.String(200))
    username            = db.Column(db.String(50), nullable=False)
    password            = db.Column(db.String(100), nullable=False)
    profile_id          = db.Column(db.Integer, db.ForeignKey('pppoe_profile.id'), nullable=False)
    tanggal_jatuh_tempo = db.Column(db.Date, nullable=False)
    status              = db.Column(db.String(20), nullable=False, default='active')
    tanggal_dibuat       = db.Column(db.Date, nullable=False)

    profile  = db.relationship('PPPoEProfile')
    payments = db.relationship('PPPoEPayment', backref='customer', cascade='all, delete-orphan')


class PPPoEPayment(db.Model):
    __tablename__ = 'pppoe_payment'
    id            = db.Column(db.Integer, primary_key=True)
    customer_id   = db.Column(db.Integer, db.ForeignKey('pppoe_customer.id'), nullable=False)
    periode       = db.Column(db.String(20))
    jumlah_bayar  = db.Column(db.Integer, nullable=False)
    tanggal_bayar = db.Column(db.Date, nullable=False)

with app.app_context():
    db.create_all()

VALID_USERNAME = os.getenv('ADMIN_USERNAME')
VALID_PASSWORD = os.getenv('ADMIN_PASSWORD')

# --- MIDDLEWARE ---
def login_required(func):
    def wrapper(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return func(*args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper

def router_connected_required(func):
    def wrapper(*args, **kwargs):
        if 'connected_router_id' not in session:
            flash('Anda harus terkoneksi ke router terlebih dahulu!', 'danger')
            return redirect(url_for('admin_setting'))
        return func(*args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper

# --- HELPER CONNECTION ---
def get_mikrotik_api():
    router_id = session.get('connected_router_id')
    
    # Jika tidak ada sesi ID, langsung kembalikan None
    if not router_id:
        return None, None
        
    router = Router.query.get(router_id)
    if not router:
        return None, None
        
    try:
        connection = routeros_api.RouterOsApiPool(
            router.ip, 
            username=router.username, 
            password=router.password, 
            plaintext_login=True
        )
        api = connection.get_api()
        return api, connection
    except Exception as e:
        # KUNCI PERBAIKAN: Jika koneksi gagal, hapus sesi palsu/hantu dari browser
        session.pop('connected_router_id', None)
        session.pop('connected_router_name', None)
        return None, None

def format_bytes(size_bytes):
    if size_bytes == 0:
        return "0 B"
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    i = 0
    while size_bytes >= 1024 and i < len(units) - 1:
        size_bytes /= 1024.0
        i += 1
    return f"{round(size_bytes, 1)} {units[i]}"

# --- ROUTES AWAL & AUTH ---
@app.route('/', methods=['GET', 'POST'])
@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'logged_in' in session:
        return redirect(url_for('admin_setting'))
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username == VALID_USERNAME and password == VALID_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('admin_setting'))
        else:
            flash('Username atau password salah!', 'danger')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/admin-setting')
@login_required
def admin_setting():
    # KUNCI PERBAIKAN: Otomatis putuskan koneksi router saat masuk ke halaman setting
    if 'connected_router_id' in session:
        session.pop('connected_router_id', None)
        session.pop('connected_router_name', None)
        
    routers = Router.query.all()
    return render_template('admin_setting.html', routers=routers)

@app.route('/add-router', methods=['GET', 'POST'])
@login_required
def add_router():
    if request.method == 'POST':
        new_router = Router(
            name=request.form.get('name'), 
            ip=request.form.get('ip'), 
            username=request.form.get('username'), 
            password=request.form.get('password')
        )
        try:
            db.session.add(new_router)
            db.session.commit()
            flash('Router berhasil ditambahkan!', 'success')
            return redirect(url_for('admin_setting'))
        except:
            db.session.rollback()
            flash('Gagal menambahkan router.', 'danger')
    return render_template('add_router.html')

@app.route('/delete-router/<int:router_id>', methods=['POST'])
@login_required
def delete_router(router_id):
    router = Router.query.get_or_404(router_id)
    db.session.delete(router)
    db.session.commit()
    flash(f"Router '{router.name}' berhasil dihapus!", "success")
    return redirect(url_for('admin_setting'))

@app.route('/about')
@login_required
def about():
    return render_template('about.html')

# ==========================================
# FITUR MIKROTIK
# ==========================================

@app.route('/connect/<int:router_id>')
@login_required
def connect_router(router_id):
    router = Router.query.get_or_404(router_id)
    try:
        pool = routeros_api.RouterOsApiPool(router.ip, username=router.username, password=router.password, plaintext_login=True)
        api = pool.get_api()
        pool.disconnect()
        
        session['connected_router_id'] = router.id
        session['connected_router_name'] = router.name
        return redirect(url_for('dashboard'))
    except Exception as e:
        flash(f"Gagal terhubung ke MikroTik {router.name}. Cek IP dan Kredensial.", "danger")
        return redirect(url_for('admin_setting'))

@app.route('/disconnect-router')
@login_required
def disconnect_router():
    session.pop('connected_router_id', None)
    session.pop('connected_router_name', None)
    flash("Koneksi ke router diputuskan.", "success")
    return redirect(url_for('admin_setting'))

@app.route('/dashboard')
@login_required
@router_connected_required
def dashboard():
    api, connection = get_mikrotik_api()
    router_info = {'board_name': 'MikroTik', 'model': 'Unknown', 'version': 'Unknown'}
    perf_info = {'cpu_load': '0', 'free_memory': '0 MB', 'free_hdd': '0 MB'}
    hotspot_info = {'active': '0', 'users_count': '0'}

    if api:
        try:
            resource_data = api.get_resource('/system/resource').get()
            if resource_data:
                res = resource_data[0]
                router_info['board_name'] = res.get('board-name', 'MikroTik')
                router_info['model'] = res.get('model', 'Unknown')
                router_info['version'] = res.get('version', 'Unknown')
                perf_info['cpu_load'] = res.get('cpu-load', '0')
                
                free_mem_bytes = int(res.get('free-memory', 0))
                free_hdd_bytes = int(res.get('free-hdd-space', 0))
                perf_info['free_memory'] = f"{round(free_mem_bytes / (1024 * 1024), 1)} MB"
                perf_info['free_hdd'] = f"{round(free_hdd_bytes / (1024 * 1024), 1)} MB"

            hotspot_active_list = api.get_resource('/ip/hotspot/active').get()
            hotspot_info['active'] = len(hotspot_active_list)

            hotspot_user_list = api.get_resource('/ip/hotspot/user').get()
            hotspot_info['users_count'] = len(hotspot_user_list)

        except Exception as e:
            flash(f"Gagal memperbarui data dashboard: {e}", "danger")
        finally:
            connection.disconnect()

    return render_template('dashboard.html', router=router_info, perf=perf_info, hotspot=hotspot_info)

@app.route('/interfaces')
@login_required
@router_connected_required
def interfaces():
    api, connection = get_mikrotik_api()
    if not api:
        flash("Koneksi terputus.", "danger")
        return redirect(url_for('disconnect_router'))
    
    interface_list = api.get_resource('/interface').get()
    connection.disconnect()
    return render_template('interfaces.html', interfaces=interface_list)

@app.route('/ip-address')
@login_required
@router_connected_required
def ip_address():
    api, connection = get_mikrotik_api()
    if not api:
        flash("Koneksi terputus.", "danger")
        return redirect(url_for('disconnect_router'))
    
    ip_list = api.get_resource('/ip/address').get()
    interface_list = api.get_resource('/interface').get()
    connection.disconnect()
    return render_template('ip_address.html', ips=ip_list, interfaces=interface_list)

@app.route('/ip-address/add', methods=['POST'])
@login_required
@router_connected_required
def add_ip():
    address = request.form.get('address')
    interface = request.form.get('interface')
    api, connection = get_mikrotik_api()
    if api:
        try:
            api.get_resource('/ip/address').add(address=address, interface=interface)
            flash(f"IP {address} berhasil ditambahkan!", "success")
        except Exception as e:
            flash(f"Gagal menambah IP: {e}", "danger")
        connection.disconnect()
    return redirect(url_for('ip_address'))

@app.route('/ip-address/edit', methods=['POST'])
@login_required
@router_connected_required
def edit_ip():
    ip_id = request.form.get('id')
    address = request.form.get('address')
    interface = request.form.get('interface')
    api, connection = get_mikrotik_api()
    if api:
        try:
            api.get_resource('/ip/address').set(id=ip_id, address=address, interface=interface)
            flash(f"IP berhasil diubah menjadi {address}!", "success")
        except Exception as e:
            flash(f"Gagal mengubah IP: {e}", "danger")
        connection.disconnect()
    return redirect(url_for('ip_address'))

@app.route('/ip-address/delete', methods=['POST'])
@login_required
@router_connected_required
def delete_ip():
    ip_id = request.form.get('id')
    api, connection = get_mikrotik_api()
    if api:
        try:
            api.get_resource('/ip/address').remove(id=ip_id)
            flash("IP berhasil dihapus!", "success")
        except Exception as e:
            flash(f"Gagal menghapus IP: {e}", "danger")
        connection.disconnect()
    return redirect(url_for('ip_address'))

#Route connect internet
@app.route('/connect-internet', methods=['GET', 'POST'])
@login_required
@router_connected_required
def connect_internet():

    # GET: tampilkan form, ambil list interface dari MikroTik
    if request.method == 'GET':
        api, connection = get_mikrotik_api()
        interfaces = []
        if api:
            try:
                interfaces = api.get_resource('/interface').get()
            except Exception as e:
                flash(f'Gagal memuat interface: {e}', 'danger')
            finally:
                connection.disconnect()
        return render_template('connect_internet.html', interfaces=interfaces)

    # POST: proses konfigurasi
    conn_type = request.form.get('type')
    interface = request.form.get('interface')

    if not interface or not conn_type:
        flash('Interface dan tipe koneksi wajib dipilih.', 'danger')
        return redirect(url_for('connect_internet'))

    api, connection = get_mikrotik_api()
    if not api:
        flash('Koneksi ke router gagal.', 'danger')
        return redirect(url_for('connect_internet'))

    try:
        if conn_type == 'dhcp':
            _setup_dhcp(api, interface)

        elif conn_type == 'static':
            ip      = request.form.get('static_ip')
            gateway = request.form.get('static_gateway')
            dns1    = request.form.get('static_dns1')
            dns2    = request.form.get('static_dns2', '')
            if not ip or not gateway or not dns1:
                flash('IP Address, Gateway, dan DNS 1 wajib diisi untuk Static IP.', 'danger')
                return redirect(url_for('connect_internet'))
            _setup_static(api, interface, ip, gateway, dns1, dns2)

        elif conn_type == 'pppoe':
            username = request.form.get('pppoe_user')
            password = request.form.get('pppoe_pass')
            service  = request.form.get('pppoe_service', '')
            dns1     = request.form.get('pppoe_dns1', '')
            dns2     = request.form.get('pppoe_dns2', '')
            if not username or not password:
                flash('Username dan Password wajib diisi untuk PPPoE.', 'danger')
                return redirect(url_for('connect_internet'))
            _setup_pppoe(api, interface, username, password, service, dns1, dns2)

        flash(f'Koneksi {conn_type.upper()} pada interface {interface} berhasil diterapkan!', 'success')
        return redirect(url_for('ip_address'))

    except Exception as e:
        flash(f'Gagal menerapkan konfigurasi: {e}', 'danger')
        return redirect(url_for('connect_internet'))
    finally:
        connection.disconnect()


def _setup_dhcp(api, interface):
    """Buat DHCP client pada interface yang dipilih."""
    # Hapus DHCP client lama di interface ini jika ada
    existing = api.get_resource('/ip/dhcp-client').get()
    for item in existing:
        if item.get('interface') == interface:
            api.get_resource('/ip/dhcp-client').remove(id=item.get('.id'))

    api.get_resource('/ip/dhcp-client').add(
        interface=interface,
        disabled='no'
    )


def _setup_static(api, interface, ip, gateway, dns1, dns2):
    """Set IP statis, default route, dan DNS."""
    # Hapus IP lama di interface ini
    existing_ips = api.get_resource('/ip/address').get()
    for item in existing_ips:
        if item.get('interface') == interface:
            api.get_resource('/ip/address').remove(id=item.get('.id'))

    # Tambah IP baru
    api.get_resource('/ip/address').add(
        address=ip,
        interface=interface
    )

    # Hapus default route lama jika ada
    existing_routes = api.get_resource('/ip/route').get()
    for item in existing_routes:
        if item.get('dst-address') == '0.0.0.0/0':
            api.get_resource('/ip/route').remove(id=item.get('.id'))

    # Tambah default gateway
    api.get_resource('/ip/route').add(**{
        'dst-address': '0.0.0.0/0',
        'gateway': gateway
    })

    # Set DNS
    dns_servers = dns1
    if dns2:
        dns_servers += f',{dns2}'
    api.get_resource('/ip/dns').set(**{
        'servers': dns_servers,
        'allow-remote-requests': 'yes'
    })


def _setup_pppoe(api, interface, username, password, service, dns1, dns2):
    """Buat PPPoE client pada interface yang dipilih."""
    # Hapus PPPoE client lama di interface ini jika ada
    existing = api.get_resource('/interface/pppoe-client').get()
    for item in existing:
        if item.get('interface') == interface:
            api.get_resource('/interface/pppoe-client').remove(id=item.get('.id'))

    pppoe_data = {
        'name':           f'pppoe-{interface}',
        'interface':      interface,
        'user':           username,
        'password':       password,
        'disabled':       'no',
        'add-default-route': 'yes',
        'use-peer-dns':   'yes' if not dns1 else 'no',
    }
    if service:
        pppoe_data['service-name'] = service

    api.get_resource('/interface/pppoe-client').add(**pppoe_data)

    # Set DNS manual jika diisi
    if dns1:
        dns_servers = dns1
        if dns2:
            dns_servers += f',{dns2}'
        api.get_resource('/ip/dns').set(**{
            'servers': dns_servers,
            'allow-remote-requests': 'yes'
        })

LB_TAG = 'AUTO-LB-EINSTEIN'  # penanda khusus supaya hanya rule milik fitur ini yang dihapus saat reset


@app.route('/load-balance')
@login_required
@router_connected_required
def load_balance():
    api, connection = get_mikrotik_api()
    interfaces = []
    if api:
        try:
            interfaces = api.get_resource('/interface').get()
        except Exception as e:
            flash(f'Gagal memuat interface: {e}', 'danger')
        finally:
            connection.disconnect()
    return render_template('load_balance.html', interfaces=interfaces)


@app.route('/load-balance/save', methods=['POST'])
@login_required
@router_connected_required
def save_load_balance():
    wan_interfaces = request.form.getlist('wan_interface[]')
    wan_gateways   = request.form.getlist('wan_gateway[]')
    wan_weights    = request.form.getlist('wan_weight[]')
    pcc_mode       = request.form.get('pcc_mode', 'both')
    lan_network    = request.form.get('lan_network')
    failover_on    = request.form.get('failover_enabled') == 'on'

    if len(wan_interfaces) < 2:
        flash('Minimal 2 WAN diperlukan untuk Load Balance.', 'danger')
        return redirect(url_for('load_balance'))

    if not lan_network:
        flash('Local Network/LAN wajib diisi.', 'danger')
        return redirect(url_for('load_balance'))

    api, connection = get_mikrotik_api()
    if not api:
        flash('Koneksi ke router gagal.', 'danger')
        return redirect(url_for('load_balance'))

    router_id = session.get('connected_router_id')

    try:
        _reset_load_balance(api)
        _apply_pcc(api, wan_interfaces, wan_gateways, wan_weights, pcc_mode, lan_network, failover_on)

        # Hapus konfigurasi lama di DB untuk router ini (kalau ada), lalu simpan yang baru
        old_config = LoadBalanceConfig.query.filter_by(router_id=router_id).first()
        if old_config:
            db.session.delete(old_config)
            db.session.commit()

        new_config = LoadBalanceConfig(
            router_id=router_id,
            pcc_mode=pcc_mode,
            lan_network=lan_network,
            failover_enabled=failover_on
        )
        db.session.add(new_config)
        db.session.flush()  # supaya new_config.id terisi sebelum dipakai WanEntry

        for i, (iface, gw, w) in enumerate(zip(wan_interfaces, wan_gateways, wan_weights)):
            db.session.add(WanEntry(
                config_id=new_config.id,
                interface=iface,
                gateway=gw,
                weight=int(w) if w else 1,
                priority=i + 1
            ))
        db.session.commit()

        flash(f'Load Balance berhasil diterapkan untuk {len(wan_interfaces)} WAN!', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Gagal menerapkan Load Balance: {e}', 'danger')
    finally:
        connection.disconnect()

    return redirect(url_for('info_load_balance'))


def _reset_load_balance(api):
    """Hapus semua rule yang sebelumnya dibuat oleh fitur ini (ditandai via comment LB_TAG)."""
    targets = [
        ('/ip/firewall/mangle', api.get_resource('/ip/firewall/mangle')),
        ('/ip/firewall/nat',    api.get_resource('/ip/firewall/nat')),
        ('/ip/route',           api.get_resource('/ip/route')),
    ]
    for _, resource in targets:
        for item in resource.get():
            if item.get('comment', '').startswith(LB_TAG):
                resource.remove(id=item.get('.id'))


def _apply_pcc(api, interfaces, gateways, weights, pcc_mode, lan_network, failover_on):
    mangle  = api.get_resource('/ip/firewall/mangle')
    nat     = api.get_resource('/ip/firewall/nat')
    route   = api.get_resource('/ip/route')

    pcc_classifier = {
        'both': 'both-addresses',
        'src':  'src-address',
        'dst':  'dst-address',
    }.get(pcc_mode, 'both-addresses')

    total_wan = len(interfaces)

    for i, (iface, gw) in enumerate(zip(interfaces, gateways)):
        wan_num    = i + 1
        mark_conn  = f'wan{wan_num}-conn'
        mark_route = f'wan{wan_num}-route'

        # 1) Mangle: tandai koneksi baru dari LAN sesuai pembagian PCC
        mangle.add(**{
            'chain':            'prerouting',
            'src-address':      lan_network,
            'in-interface':     'all',
            'connection-state': 'new',
            'per-connection-classifier': f'{pcc_classifier}:{total_wan}/{i}',
            'action':           'mark-connection',
            'new-connection-mark': mark_conn,
            'passthrough':      'yes',
            'comment':          f'{LB_TAG} mangle-conn wan{wan_num}'
        })

        # 2) Mangle: tandai routing berdasarkan connection mark di atas
        mangle.add(**{
            'chain':                'prerouting',
            'connection-mark':      mark_conn,
            'action':               'mark-routing',
            'new-routing-mark':     mark_route,
            'passthrough':          'yes',
            'comment':              f'{LB_TAG} mangle-route wan{wan_num}'
        })

        # 3) NAT: masquerade keluar lewat interface WAN ini
        nat.add(**{
            'chain':        'srcnat',
            'out-interface': iface,
            'action':       'masquerade',
            'comment':      f'{LB_TAG} nat wan{wan_num}'
        })

        # 4) Route dengan routing-mark sesuai WAN, plus check-gateway untuk monitoring
        route.add(**{
            'dst-address':    '0.0.0.0/0',
            'gateway':        gw,
            'routing-mark':   mark_route,
            'check-gateway':  'ping',
            'distance':       '1',
            'comment':        f'{LB_TAG} route wan{wan_num}'
        })

        # 5) Route failover: default route biasa, distance bertingkat sesuai urutan WAN
        #    WAN pertama distance=1 (utama), WAN berikutnya distance lebih besar (cadangan)
        if failover_on:
            route.add(**{
                'dst-address':   '0.0.0.0/0',
                'gateway':       gw,
                'check-gateway': 'ping',
                'distance':      str(wan_num),
                'comment':       f'{LB_TAG} failover wan{wan_num}'
            })

PCC_MODE_LABELS = {
    'both': 'Source & Destination',
    'src':  'Source Address',
    'dst':  'Destination Address',
}

@app.route('/load-balance/info')
@login_required
@router_connected_required
def info_load_balance():
    router_id = session.get('connected_router_id')
    config = LoadBalanceConfig.query.filter_by(router_id=router_id).first()

    lb_config = None
    if config:
        # Ambil status interface terbaru dari MikroTik untuk ditampilkan di kolom Status
        running_map = {}
        api, connection = get_mikrotik_api()
        if api:
            try:
                ifaces = api.get_resource('/interface').get()
                running_map = {i.get('name'): i.get('running') == 'true' for i in ifaces}
            except Exception:
                pass
            finally:
                connection.disconnect()

        wan_list = []
        for wan in config.wans:
            wan_list.append({
                'interface': wan.interface,
                'gateway':   wan.gateway,
                'weight':    wan.weight,
                'running':   running_map.get(wan.interface, False)
            })

        lb_config = {
            'pcc_mode_label':   PCC_MODE_LABELS.get(config.pcc_mode, config.pcc_mode),
            'lan_network':      config.lan_network,
            'failover_enabled': config.failover_enabled,
            'wan_list':         wan_list
        }

    return render_template('info_load_balance.html', lb_config=lb_config)

@app.route('/load-balance/delete', methods=['POST'])
@login_required
@router_connected_required
def delete_load_balance():
    router_id = session.get('connected_router_id')

    api, connection = get_mikrotik_api()
    if api:
        try:
            _reset_load_balance(api)
        except Exception as e:
            flash(f'Gagal menghapus rule di router: {e}', 'danger')
        finally:
            connection.disconnect()

    config = LoadBalanceConfig.query.filter_by(router_id=router_id).first()
    if config:
        db.session.delete(config)
        db.session.commit()

    flash('Konfigurasi Load Balance berhasil dihapus.', 'success')
    return redirect(url_for('info_load_balance'))

# ==========================================
# HOTSPOT USER
# ==========================================

@app.route('/hotspot/users')
@login_required
@router_connected_required
def hotspot_user_list():
    api, connection = get_mikrotik_api()
    formatted_users = []
    profiles = []
    comments = set()

    if api:
        try:
            raw_users = api.get_resource('/ip/hotspot/user').get()
            raw_profiles = api.get_resource('/ip/hotspot/user/profile').get()
            profiles = [p['name'] for p in raw_profiles if 'name' in p]
            
            for user in raw_users:
                bytes_in = int(user.get('bytes-in', 0))
                bytes_out = int(user.get('bytes-out', 0))
                user['bytes-in-formatted'] = format_bytes(bytes_in)
                user['bytes-out-formatted'] = format_bytes(bytes_out)
                
                if 'comment' in user:
                    comments.add(user['comment'])
                
                formatted_users.append(user)
                
        except Exception as e:
            flash(f"Gagal memuat daftar user: {e}", "danger")
        finally:
            connection.disconnect()

    comments_list = sorted(list(comments))
    return render_template('hotspot_user_list.html', users=formatted_users, profiles=profiles, comments=comments_list)

@app.route('/hotspot/user/delete', methods=['POST'])
@login_required
@router_connected_required
def delete_hotspot_user():
    user_id = request.form.get('id')
    api, connection = get_mikrotik_api()

    if api and user_id:
        try:
            api.get_resource('/ip/hotspot/user').remove(id=user_id)
            flash("User hotspot berhasil dihapus secara permanen!", "success")
        except Exception as e:
            flash(f"Gagal menghapus user: {e}", "danger")
        finally:
            connection.disconnect()
            
    return redirect(url_for('hotspot_user_list'))

# ==========================================
# HOTSPOT PROFILE
# ==========================================

@app.route('/hotspot/profile/add', methods=['GET', 'POST'])
@login_required
@router_connected_required
def add_hotspot_profile():
    if request.method == 'POST':
        name = request.form.get('name')
        pool = request.form.get('address_pool')
        shared_users = request.form.get('shared_users')
        rate_limit = request.form.get('rate_limit')
        parent_queue = request.form.get('parent_queue')
        
        # validity, expired_mode, harga, lock_user belum difungsikan

        api, connection = get_mikrotik_api()
        if api:
            try:
                profile_data = {
                    'name': name,
                }
                
                if pool and pool != 'none':
                    profile_data['address-pool'] = pool
                if shared_users:
                    profile_data['shared-users'] = shared_users
                if rate_limit:
                    profile_data['rate-limit'] = rate_limit
                if parent_queue and parent_queue != 'none':
                    profile_data['parent-queue'] = parent_queue
                
                api.get_resource('/ip/hotspot/user/profile').add(**profile_data)
                return redirect(url_for('hotspot_profile_list'))
                
            except Exception as e:
                flash(f"Gagal menambahkan profile: {e}", "danger")
            finally:
                connection.disconnect()
        else:
            flash("Koneksi ke router gagal.", "danger")

    # GET request — buka koneksi baru khusus untuk mengambil data pools & queues
    pools = []
    queues = []
    api, connection = get_mikrotik_api()
    if api:
        try:
            pools = api.get_resource('/ip/pool').get()
            queues = api.get_resource('/queue/simple').get()
        except Exception as e:
            flash(f"Gagal memuat data: {e}", "danger")
        finally:
            connection.disconnect()

    return render_template('hotspot_profile_add.html', pools=pools, queues=queues)

@app.route('/hotspot/profiles')
@login_required
@router_connected_required
def hotspot_profile_list():
    api, connection = get_mikrotik_api()
    formatted_profiles = []

    if api:
        try:
            raw_profiles = api.get_resource('/ip/hotspot/user/profile').get()
            
            mode_map = {
                '0': 'None', 
                'rem': 'Remove', 
                'ntc': 'Notice', 
                'remc': 'Remove & Record', 
                'ntcc': 'Notice & Record'
            }

            for profile in raw_profiles:
                # Normalisasi key ID dari MikroTik (bisa '.id' atau 'id')
                profile['profile_id'] = profile.get('.id') or profile.get('id') or ''
                profile['validity'] = '-'
                profile['expired_mode'] = 'None'
                profile['price'] = '-'
                profile['lock_user'] = 'Disable'
                
                comment = profile.get('comment', '')
                
                if comment and 'validity=' in comment:
                    try:
                        parts = comment.split(',')
                        for part in parts:
                            if '=' in part:
                                key, value = part.split('=', 1)
                                if key == 'validity':
                                    profile['validity'] = value
                                elif key == 'expired_mode':
                                    profile['expired_mode'] = mode_map.get(value, 'None')
                                elif key == 'price':
                                    profile['price'] = f"{int(value):,}".replace(',', '.') if value.isdigit() and int(value) > 0 else '-'
                                elif key == 'lock_user':
                                    profile['lock_user'] = value
                    except Exception:
                        pass
                
                formatted_profiles.append(profile)
                
        except Exception as e:
            flash(f"Gagal memuat daftar profile: {e}", "danger")
        finally:
            connection.disconnect()

    return render_template('hotspot_profile_list.html', profiles=formatted_profiles)

@app.route('/hotspot/profile/delete', methods=['POST'])
@login_required
@router_connected_required
def delete_hotspot_profile():
    profile_id = request.form.get('id')
    
    if not profile_id:
        flash("ID profile tidak ditemukan.", "danger")
        return redirect(url_for('hotspot_profile_list'))

    api, connection = get_mikrotik_api()

    if api:
        try:
            api.get_resource('/ip/hotspot/user/profile').remove(id=profile_id)
        except Exception as e:
            flash(f"Gagal menghapus profile: {e}", "danger")
        finally:
            connection.disconnect()
    else:
        flash("Koneksi ke router gagal.", "danger")
            
    return redirect(url_for('hotspot_profile_list'))

@app.route('/hotspot/profile/edit', methods=['GET', 'POST'])
@login_required
@router_connected_required
def edit_hotspot_profile():

    # POST: simpan perubahan ke MikroTik
    if request.method == 'POST':
        profile_id   = request.form.get('id')
        name         = request.form.get('name')
        pool         = request.form.get('address_pool')
        shared_users = request.form.get('shared_users')
        rate_limit   = request.form.get('rate_limit')
        parent_queue = request.form.get('parent_queue')

        api, connection = get_mikrotik_api()
        if api:
            try:
                update_data = {'.id': profile_id, 'name': name}
                update_data['address-pool'] = pool         if pool         and pool         != 'none' else 'none'
                update_data['parent-queue'] = parent_queue if parent_queue and parent_queue != 'none' else 'none'
                update_data['rate-limit']   = rate_limit   if rate_limit   else ''
                if shared_users:
                    update_data['shared-users'] = shared_users

                api.get_resource('/ip/hotspot/user/profile').set(**update_data)
                flash(f"Profile '{name}' berhasil diperbarui!", 'success')
                return redirect(url_for('hotspot_profile_list'))
            except Exception as e:
                flash(f'Gagal memperbarui profile: {e}', 'danger')
            finally:
                connection.disconnect()
        else:
            flash('Koneksi ke router gagal.', 'danger')
        return redirect(url_for('hotspot_profile_list'))

    # GET: ambil data profile lalu tampilkan form edit
    profile_id = request.args.get('id')
    if not profile_id:
        flash('ID profile tidak ditemukan.', 'danger')
        return redirect(url_for('hotspot_profile_list'))

    profile = None
    pools   = []
    queues  = []

    api, connection = get_mikrotik_api()
    if api:
        try:
            all_profiles = api.get_resource('/ip/hotspot/user/profile').get()
            for p in all_profiles:
                pid = p.get('.id') or p.get('id') or ''
                if pid == profile_id:
                    p['.id'] = pid
                    profile = p
                    break
            pools  = api.get_resource('/ip/pool').get()
            queues = api.get_resource('/queue/simple').get()
        except Exception as e:
            flash(f'Gagal memuat data profile: {e}', 'danger')
        finally:
            connection.disconnect()

    if not profile:
        flash('Profile tidak ditemukan.', 'danger')
        return redirect(url_for('hotspot_profile_list'))

    return render_template('hotspot_profile_edit.html', profile=profile, pools=pools, queues=queues)

# ==========================================
# HOTSPOT ACTIVE
# ==========================================

@app.route('/hotspot/active')
@login_required
@router_connected_required
def hotspot_active_list():
    api, connection = get_mikrotik_api()
    formatted_actives = []

    if api:
        try:
            raw_actives = api.get_resource('/ip/hotspot/active').get()
            
            for active in raw_actives:
                bytes_in = int(active.get('bytes-in', 0))
                bytes_out = int(active.get('bytes-out', 0))
                
                active['bytes-in-formatted'] = format_bytes(bytes_in)
                active['bytes-out-formatted'] = format_bytes(bytes_out)
                
                formatted_actives.append(active)
                
        except Exception as e:
            flash(f"Gagal memuat daftar user aktif: {e}", "danger")
        finally:
            connection.disconnect()

    return render_template('hotspot_active_list.html', actives=formatted_actives)

@app.route('/hotspot/active/delete', methods=['POST'])
@login_required
@router_connected_required
def delete_hotspot_active():
    active_id = request.form.get('id')
    api, connection = get_mikrotik_api()

    if api and active_id:
        try:
            api.get_resource('/ip/hotspot/active').remove(id=active_id)
            flash("Koneksi user berhasil diputus!", "success")
        except Exception as e:
            flash(f"Gagal memutus user: {e}", "danger")
        finally:
            connection.disconnect()
            
    return redirect(url_for('hotspot_active_list'))

# ==========================================
# PPPoE - DASHBOARD
# ==========================================

@app.route('/pppoe/dashboard')
@login_required
@router_connected_required
def pppoe_dashboard():
    router_id = session.get('connected_router_id')
    today     = date.today()

    customers = PPPoECustomer.query.filter_by(router_id=router_id).all()

    total_customers    = len(customers)
    active_customers   = sum(1 for c in customers if c.status == 'active')
    isolated_customers = sum(1 for c in customers if c.status == 'isolated')

    # Pemasukan bulan ini (jumlahkan payment dengan tanggal_bayar di bulan & tahun berjalan)
    income_this_month = db.session.query(func.sum(PPPoEPayment.jumlah_bayar)).join(PPPoECustomer).filter(
        PPPoECustomer.router_id == router_id,
        func.strftime('%Y-%m', PPPoEPayment.tanggal_bayar) == today.strftime('%Y-%m')
    ).scalar() or 0

    stats = {
        'total_customers':    total_customers,
        'active_customers':   active_customers,
        'isolated_customers': isolated_customers,
        'income_this_month':  income_this_month
    }

    # Grafik pemasukan 6 bulan terakhir
    chart_labels = []
    chart_income = []
    bulan_id = ['Jan','Feb','Mar','Apr','Mei','Jun','Jul','Agu','Sep','Okt','Nov','Des']

    for i in range(5, -1, -1):
        target_month = today - relativedelta(months=i)
        ym = target_month.strftime('%Y-%m')
        total = db.session.query(func.sum(PPPoEPayment.jumlah_bayar)).join(PPPoECustomer).filter(
            PPPoECustomer.router_id == router_id,
            func.strftime('%Y-%m', PPPoEPayment.tanggal_bayar) == ym
        ).scalar() or 0
        chart_labels.append(bulan_id[target_month.month - 1])
        chart_income.append(total)

    # Pelanggan yang perlu perhatian: isolir atau jatuh tempo ≤ 3 hari ke depan
    attention_threshold = today + timedelta(days=3)
    attention_customers = PPPoECustomer.query.filter(
        PPPoECustomer.router_id == router_id,
        db.or_(
            PPPoECustomer.status == 'isolated',
            PPPoECustomer.tanggal_jatuh_tempo <= attention_threshold
        )
    ).order_by(PPPoECustomer.tanggal_jatuh_tempo.asc()).all()

    attention_list = [{
        'nama_pelanggan':      c.nama_pelanggan,
        'username':            c.username,
        'profile_name':        c.profile.nama if c.profile else '-',
        'tanggal_jatuh_tempo': c.tanggal_jatuh_tempo.strftime('%d %b %Y'),
        'status':              c.status
    } for c in attention_customers]

    return render_template('PPPoE/pppoe_dashboard.html',
        stats=stats,
        chart_labels=chart_labels,
        chart_income=chart_income,
        attention_list=attention_list
    )

# ==========================================
# PPPoE - DAFTAR PELANGGAN
# ==========================================

@app.route('/pppoe/pelanggan')
@login_required
@router_connected_required
def daftar_pelanggan():
    router_id = session.get('connected_router_id')
    customers = PPPoECustomer.query.filter_by(router_id=router_id).order_by(
        PPPoECustomer.tanggal_jatuh_tempo.asc()
    ).all()

    return render_template('PPPoE/daftar_pelanggan.html', customers=customers)


@app.route('/pppoe/pelanggan/tandai-lunas', methods=['POST'])
@login_required
@router_connected_required
def tandai_lunas():
    customer_id   = request.form.get('customer_id')
    jumlah_bayar  = request.form.get('jumlah_bayar')
    tanggal_bayar = request.form.get('tanggal_bayar')

    if not customer_id or not jumlah_bayar or not tanggal_bayar:
        flash('Data pembayaran tidak lengkap.', 'danger')
        return redirect(url_for('daftar_pelanggan'))

    customer = PPPoECustomer.query.get(customer_id)
    if not customer:
        flash('Pelanggan tidak ditemukan.', 'danger')
        return redirect(url_for('daftar_pelanggan'))

    try:
        tgl_bayar = datetime.strptime(tanggal_bayar, '%Y-%m-%d').date()

        # Simpan riwayat pembayaran
        payment = PPPoEPayment(
            customer_id=customer.id,
            periode=tgl_bayar.strftime('%Y-%m'),
            jumlah_bayar=int(jumlah_bayar),
            tanggal_bayar=tgl_bayar
        )
        db.session.add(payment)

        # Jatuh tempo baru = tanggal bayar + 1 bulan (sesuai kesepakatan: geser, bukan tetap)
        customer.tanggal_jatuh_tempo = tgl_bayar + relativedelta(months=1)

        was_isolated = customer.status == 'isolated'
        customer.status = 'active'

        db.session.commit()

        # Kalau sebelumnya diisolir, kembalikan ke profile normal di MikroTik
        if was_isolated:
            api, connection = get_mikrotik_api()
            if api:
                try:
                    secrets = list(api.get_resource('/ppp/secret').get())
                    for s in secrets:
                        if s.get('name') == customer.username:
                            mikrotik_id = s.get('.id') or s.get('id')
                            if mikrotik_id:
                                api.get_resource('/ppp/secret').set(
                                    id=mikrotik_id,
                                    profile=customer.profile.nama,
                                    disabled='no'
                                )
                            break
                except Exception as e:
                    flash(f'Pembayaran tersimpan, tapi gagal sinkron ke router: {e}', 'danger')
                finally:
                    connection.disconnect()

        flash(f'Pembayaran {customer.nama_pelanggan} berhasil dicatat. Jatuh tempo baru: {customer.tanggal_jatuh_tempo.strftime("%d %b %Y")}.', 'success')

    except Exception as e:
        db.session.rollback()
        flash(f'Gagal mencatat pembayaran: {e}', 'danger')

    return redirect(url_for('daftar_pelanggan'))


@app.route('/pppoe/pelanggan/hapus', methods=['POST'])
@login_required
@router_connected_required
def hapus_pelanggan():
    customer_id = request.form.get('id')

    if not customer_id:
        flash('ID pelanggan tidak ditemukan.', 'danger')
        return redirect(url_for('daftar_pelanggan'))

    customer = PPPoECustomer.query.get(customer_id)
    if not customer:
        flash('Pelanggan tidak ditemukan.', 'danger')
        return redirect(url_for('daftar_pelanggan'))

    # Hapus secret PPPoE di MikroTik dulu
    api, connection = get_mikrotik_api()
    if api:
        try:
            secrets = list(api.get_resource('/ppp/secret').get())
            for s in secrets:
                if s.get('name') == customer.username:
                    mikrotik_id = s.get('.id') or s.get('id')
                    if mikrotik_id:
                        api.get_resource('/ppp/secret').remove(id=mikrotik_id)
                    break
        except Exception as e:
            flash(f'Gagal menghapus akun di router: {e}', 'danger')
        finally:
            connection.disconnect()

    try:
        db.session.delete(customer)  # payments ikut terhapus (cascade)
        db.session.commit()
        flash(f'Pelanggan {customer.nama_pelanggan} berhasil dihapus.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Gagal menghapus dari database: {e}', 'danger')

    return redirect(url_for('daftar_pelanggan'))

@app.route('/pppoe/pelanggan/tambah', methods=['GET', 'POST'])
@login_required
@router_connected_required
def tambah_pelanggan():
    router_id = session.get('connected_router_id')

    if request.method == 'POST':
        nama_pelanggan = request.form.get('nama_pelanggan')
        no_hp          = request.form.get('no_hp')
        alamat         = request.form.get('alamat')
        username       = request.form.get('username')
        password       = request.form.get('password')
        profile_id     = request.form.get('profile_id')
        jatuh_tempo    = request.form.get('tanggal_jatuh_tempo')

        # Validasi dasar
        if not all([nama_pelanggan, username, password, profile_id, jatuh_tempo]):
            flash('Semua field wajib diisi.', 'danger')
            return redirect(url_for('tambah_pelanggan'))

        # Cek username sudah dipakai pelanggan lain (di database) atau belum
        existing = PPPoECustomer.query.filter_by(router_id=router_id, username=username).first()
        if existing:
            flash(f"Username '{username}' sudah digunakan oleh pelanggan lain. Silakan gunakan username lain.", 'danger')
            return redirect(url_for('tambah_pelanggan'))

        profile = PPPoEProfile.query.get(profile_id)
        if not profile:
            flash('Profile yang dipilih tidak valid.', 'danger')
            return redirect(url_for('tambah_pelanggan'))

        try:
            tgl_jatuh_tempo = datetime.strptime(jatuh_tempo, '%Y-%m-%d').date()
        except ValueError:
            flash('Format tanggal jatuh tempo tidak valid.', 'danger')
            return redirect(url_for('tambah_pelanggan'))

        # Push ke MikroTik dulu — kalau gagal, jangan simpan ke DB
        api, connection = get_mikrotik_api()
        if not api:
            flash('Koneksi ke router gagal.', 'danger')
            return redirect(url_for('tambah_pelanggan'))

        try:
            # Cek juga di MikroTik, jaga-jaga kalau ada secret manual dengan nama sama
            existing_secrets = list(api.get_resource('/ppp/secret').get())
            if any(s.get('name') == username for s in existing_secrets):
                flash(f"Username '{username}' sudah terdaftar di router. Gunakan username lain.", 'danger')
                return redirect(url_for('tambah_pelanggan'))

            api.get_resource('/ppp/secret').add(
                name=username,
                password=password,
                service='pppoe',
                profile=profile.nama
            )
        except Exception as e:
            flash(f'Gagal membuat akun PPPoE di router: {e}', 'danger')
            return redirect(url_for('tambah_pelanggan'))
        finally:
            connection.disconnect()

        # Baru simpan ke database setelah sukses di MikroTik
        try:
            customer = PPPoECustomer(
                router_id=router_id,
                nama_pelanggan=nama_pelanggan,
                no_hp=no_hp,
                alamat=alamat,
                username=username,
                password=password,
                profile_id=profile.id,
                tanggal_jatuh_tempo=tgl_jatuh_tempo,
                status='active',
                tanggal_dibuat=date.today()
            )
            db.session.add(customer)
            db.session.commit()
            flash(f"Pelanggan '{nama_pelanggan}' berhasil ditambahkan!", 'success')
            return redirect(url_for('daftar_pelanggan'))

        except Exception as e:
            db.session.rollback()
            flash(f'Akun PPPoE sudah dibuat di router, tapi gagal disimpan ke database: {e}', 'danger')
            return redirect(url_for('tambah_pelanggan'))

    # GET: tampilkan form
    router_id = session.get('connected_router_id')
    profiles  = PPPoEProfile.query.filter_by(router_id=router_id).all()
    return render_template('PPPoE/tambah_pelanggan.html', profiles=profiles)

@app.route('/pppoe/pelanggan/edit/<int:id>', methods=['GET', 'POST'])
@login_required
@router_connected_required
def edit_pelanggan(id):
    router_id = session.get('connected_router_id')
    customer  = PPPoECustomer.query.get(id)

    if not customer or customer.router_id != router_id:
        flash('Pelanggan tidak ditemukan.', 'danger')
        return redirect(url_for('daftar_pelanggan'))

    if request.method == 'POST':
        nama_pelanggan = request.form.get('nama_pelanggan')
        no_hp          = request.form.get('no_hp')
        alamat         = request.form.get('alamat')
        username_baru  = request.form.get('username')
        password_baru  = request.form.get('password')  # boleh kosong
        profile_id     = request.form.get('profile_id')
        jatuh_tempo    = request.form.get('tanggal_jatuh_tempo')
        status_baru    = request.form.get('status')

        if not all([nama_pelanggan, username_baru, profile_id, jatuh_tempo, status_baru]):
            flash('Semua field wajib diisi.', 'danger')
            return redirect(url_for('edit_pelanggan', id=id))

        profile = PPPoEProfile.query.get(profile_id)
        if not profile:
            flash('Profile yang dipilih tidak valid.', 'danger')
            return redirect(url_for('edit_pelanggan', id=id))

        # Cek username baru tidak bentrok dengan pelanggan lain (kecuali dirinya sendiri)
        if username_baru != customer.username:
            bentrok = PPPoECustomer.query.filter(
                PPPoECustomer.router_id == router_id,
                PPPoECustomer.username == username_baru,
                PPPoECustomer.id != customer.id
            ).first()
            if bentrok:
                flash(f"Username '{username_baru}' sudah digunakan pelanggan lain.", 'danger')
                return redirect(url_for('edit_pelanggan', id=id))

        try:
            tgl_jatuh_tempo = datetime.strptime(jatuh_tempo, '%Y-%m-%d').date()
        except ValueError:
            flash('Format tanggal jatuh tempo tidak valid.', 'danger')
            return redirect(url_for('edit_pelanggan', id=id))

        # Sync ke MikroTik dulu — cari secret berdasarkan username LAMA
        api, connection = get_mikrotik_api()
        if not api:
            flash('Koneksi ke router gagal.', 'danger')
            return redirect(url_for('edit_pelanggan', id=id))

        try:
            secrets = list(api.get_resource('/ppp/secret').get())
            mikrotik_id = None
            for s in secrets:
                if s.get('name') == customer.username:
                    mikrotik_id = s.get('.id') or s.get('id')
                    break

            if not mikrotik_id:
                flash(f"Akun PPPoE '{customer.username}' tidak ditemukan di router. Mungkin sudah dihapus manual.", 'danger')
                return redirect(url_for('edit_pelanggan', id=id))

            update_data = {
                '.id':     mikrotik_id,
                'name':    username_baru,
                'profile': profile.nama,
                'disabled': 'yes' if status_baru == 'isolated' else 'no'
            }
            if password_baru:
                update_data['password'] = password_baru

            api.get_resource('/ppp/secret').set(**update_data)

        except Exception as e:
            flash(f'Gagal memperbarui akun di router: {e}', 'danger')
            return redirect(url_for('edit_pelanggan', id=id))
        finally:
            connection.disconnect()

        # Baru update database
        try:
            customer.nama_pelanggan      = nama_pelanggan
            customer.no_hp               = no_hp
            customer.alamat              = alamat
            customer.username            = username_baru
            if password_baru:
                customer.password = password_baru
            customer.profile_id          = profile.id
            customer.tanggal_jatuh_tempo = tgl_jatuh_tempo
            customer.status              = status_baru

            db.session.commit()
            flash(f"Pelanggan '{nama_pelanggan}' berhasil diperbarui!", 'success')
            return redirect(url_for('daftar_pelanggan'))

        except Exception as e:
            db.session.rollback()
            flash(f'Akun sudah diperbarui di router, tapi gagal disimpan ke database: {e}', 'danger')
            return redirect(url_for('edit_pelanggan', id=id))

    # GET: tampilkan form
    profiles = PPPoEProfile.query.all()
    return render_template('PPPoE/edit_pelanggan.html', customer=customer, profiles=profiles)

# ==========================================
# PPPoE - PROFILE
# ==========================================

@app.route('/pppoe/profile')
@login_required
@router_connected_required
def pppoe_profile_list():
    router_id = session.get('connected_router_id')
    profiles = PPPoEProfile.query.filter_by(router_id=router_id).all()
    return render_template('PPPoE/pppoe_profile_list.html', profiles=profiles)


@app.route('/pppoe/profile/tambah', methods=['GET', 'POST'])
@login_required
@router_connected_required
def tambah_pppoe_profile():
    router_id = session.get('connected_router_id')

    if request.method == 'POST':
        nama          = request.form.get('nama')
        rate_limit    = request.form.get('rate_limit')
        harga_bulanan = request.form.get('harga_bulanan')
        local_address = request.form.get('local_address')
        remote_pool   = request.form.get('remote_pool')

        if not nama or not harga_bulanan:
            flash('Nama profile dan harga bulanan wajib diisi.', 'danger')
            return redirect(url_for('tambah_pppoe_profile'))

        # Push ke MikroTik dulu — kalau gagal, jangan simpan ke database
        api, connection = get_mikrotik_api()
        if not api:
            flash('Koneksi ke router gagal.', 'danger')
            return redirect(url_for('tambah_pppoe_profile'))

        try:
            profile_data = {'name': nama}
            if rate_limit:
                profile_data['rate-limit'] = rate_limit
            if local_address:
                profile_data['local-address'] = local_address
            if remote_pool:
                profile_data['remote-address'] = remote_pool

            api.get_resource('/ppp/profile').add(**profile_data)
        except Exception as e:
            flash(f'Gagal membuat profile di router: {e}', 'danger')
            return redirect(url_for('tambah_pppoe_profile'))
        finally:
            connection.disconnect()

        # Baru simpan ke database setelah sukses di MikroTik
        try:
            profile = PPPoEProfile(
                router_id=router_id,
                nama=nama,
                rate_limit=rate_limit or None,
                harga_bulanan=int(harga_bulanan),
                local_address=local_address or None,
                remote_pool=remote_pool or None
            )
            db.session.add(profile)
            db.session.commit()
            flash(f"Profile '{nama}' berhasil ditambahkan!", 'success')
            return redirect(url_for('pppoe_profile_list'))
        except Exception as e:
            db.session.rollback()
            flash(f'Profile sudah dibuat di router, tapi gagal disimpan ke database: {e}', 'danger')
            return redirect(url_for('tambah_pppoe_profile'))

    # GET: ambil ip_addresses & pools dari MikroTik untuk dropdown
    ip_addresses = []
    pools        = []
    api, connection = get_mikrotik_api()
    if api:
        try:
            ip_addresses = api.get_resource('/ip/address').get()
            pools        = api.get_resource('/ip/pool').get()
        except Exception as e:
            flash(f'Gagal memuat data router: {e}', 'danger')
        finally:
            connection.disconnect()

    return render_template('PPPoE/tambah_pppoe_profile.html', ip_addresses=ip_addresses, pools=pools)


@app.route('/pppoe/profile/edit/<int:id>', methods=['GET', 'POST'])
@login_required
@router_connected_required
def edit_pppoe_profile(id):
    router_id = session.get('connected_router_id')
    profile   = PPPoEProfile.query.filter_by(id=id, router_id=router_id).first()

    if not profile:
        flash('Profile tidak ditemukan.', 'danger')
        return redirect(url_for('pppoe_profile_list'))

    if request.method == 'POST':
        nama          = request.form.get('nama')
        rate_limit    = request.form.get('rate_limit')
        harga_bulanan = request.form.get('harga_bulanan')
        local_address = request.form.get('local_address')
        remote_pool   = request.form.get('remote_pool')

        if not nama or not harga_bulanan:
            flash('Nama profile dan harga bulanan wajib diisi.', 'danger')
            return redirect(url_for('edit_pppoe_profile', id=id))

        # Push perubahan ke MikroTik dulu — cari profile lama berdasarkan nama LAMA
        api, connection = get_mikrotik_api()
        if not api:
            flash('Koneksi ke router gagal.', 'danger')
            return redirect(url_for('edit_pppoe_profile', id=id))

        try:
            existing_profiles = list(api.get_resource('/ppp/profile').get())
            
            mikrotik_id = None
            for mp in existing_profiles:
                mp_id = mp.get('.id') or mp.get('id')
                if mp.get('name') == profile.nama:
                    mikrotik_id = mp_id
                    break

            if mikrotik_id:
                update_data = {'.id': mikrotik_id, 'name': nama}
                update_data['rate-limit']     = rate_limit   if rate_limit   else ''
                update_data['local-address']  = local_address if local_address else ''
                update_data['remote-address'] = remote_pool   if remote_pool   else ''
                api.get_resource('/ppp/profile').set(**update_data)
            else:
                create_data = {'name': nama}
                if rate_limit:
                    create_data['rate-limit'] = rate_limit
                if local_address:
                    create_data['local-address'] = local_address
                if remote_pool:
                    create_data['remote-address'] = remote_pool
                api.get_resource('/ppp/profile').add(**create_data)
                flash(f"Catatan: profile '{profile.nama}' sebelumnya tidak ada di router, sekarang sudah dibuat ulang sebagai '{nama}'.", 'success')

        except Exception as e:
            flash(f'Gagal memperbarui profile di router: {e}', 'danger')
            return redirect(url_for('edit_pppoe_profile', id=id))
        finally:
            connection.disconnect()

        # Baru update database setelah sukses di MikroTik
        try:
            profile.nama          = nama
            profile.rate_limit    = rate_limit or None
            profile.harga_bulanan = int(harga_bulanan)
            profile.local_address = local_address or None
            profile.remote_pool   = remote_pool or None
            db.session.commit()
            flash(f"Profile '{nama}' berhasil diperbarui!", 'success')
            return redirect(url_for('pppoe_profile_list'))
        except Exception as e:
            db.session.rollback()
            flash(f'Profile sudah diperbarui di router, tapi gagal disimpan ke database: {e}', 'danger')
            return redirect(url_for('edit_pppoe_profile', id=id))

    # ── GET: tampilkan form edit ────────────────────────────────────
    ip_addresses = []
    pools        = []
    api, connection = get_mikrotik_api()
    if api:
        try:
            ip_addresses = api.get_resource('/ip/address').get()
            pools        = api.get_resource('/ip/pool').get()
        except Exception as e:
            flash(f'Gagal memuat data router: {e}', 'danger')
        finally:
            connection.disconnect()

    return render_template('PPPoE/edit_pppoe_profile.html', profile=profile, ip_addresses=ip_addresses, pools=pools)


@app.route('/pppoe/profile/hapus', methods=['POST'])
@login_required
@router_connected_required
def hapus_pppoe_profile():
    router_id  = session.get('connected_router_id')
    profile_id = request.form.get('id')

    if not profile_id:
        flash('ID profile tidak ditemukan.', 'danger')
        return redirect(url_for('pppoe_profile_list'))

    profile = PPPoEProfile.query.filter_by(id=profile_id, router_id=router_id).first()
    if not profile:
        flash('Profile tidak ditemukan.', 'danger')
        return redirect(url_for('pppoe_profile_list'))

    pelanggan_pakai = PPPoECustomer.query.filter_by(profile_id=profile.id).count()
    if pelanggan_pakai > 0:
        flash(f"Profile '{profile.nama}' tidak bisa dihapus karena masih digunakan oleh {pelanggan_pakai} pelanggan.", 'danger')
        return redirect(url_for('pppoe_profile_list'))

    # Hapus dari MikroTik dulu — wajib berhasil, kalau gagal jangan lanjut hapus database
    api, connection = get_mikrotik_api()
    if not api:
        flash('Koneksi ke router gagal.', 'danger')
        return redirect(url_for('pppoe_profile_list'))

    try:
        existing_profiles = list(api.get_resource('/ppp/profile').get())
        
        mikrotik_id = None
        for mp in existing_profiles:
            if mp.get('name') == profile.nama:
                mikrotik_id = mp.get('.id') or mp.get('id')
                break

        if mikrotik_id:
            api.get_resource('/ppp/profile').remove(id=mikrotik_id)
        else:
            flash(f"Catatan: profile '{profile.nama}' tidak ditemukan di router (mungkin sudah terhapus sebelumnya). Data di aplikasi tetap akan dihapus.", 'success')

    except Exception as e:
        flash(f'Gagal menghapus profile di router: {e}', 'danger')
        return redirect(url_for('pppoe_profile_list'))
    finally:
        connection.disconnect()

    try:
        db.session.delete(profile)
        db.session.commit()
        flash(f"Profile '{profile.nama}' berhasil dihapus.", 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Profile sudah dihapus di router, tapi gagal dihapus dari database: {e}', 'danger')

    return redirect(url_for('pppoe_profile_list'))

@app.route('/pppoe/riwayat-pembayaran')
@login_required
@router_connected_required
def riwayat_pembayaran():
    router_id = session.get('connected_router_id')

    payments = PPPoEPayment.query.join(PPPoECustomer).filter(
        PPPoECustomer.router_id == router_id
    ).order_by(PPPoEPayment.tanggal_bayar.desc()).all()

    total_income = sum(p.jumlah_bayar for p in payments)

    return render_template('PPPoE/riwayat_pembayaran.html', payments=payments, total_income=total_income)

# ==========================================
# UJI KONEKSI
# ==========================================

@app.route('/uji-koneksi')
@login_required
@router_connected_required
def uji_koneksi():
    return render_template('uji_koneksi.html')


@app.route('/uji-koneksi/ping', methods=['POST'])
@login_required
@router_connected_required
def uji_ping():
    host  = request.form.get('host', '8.8.8.8')
    count = request.form.get('count', '4')

    api, connection = get_mikrotik_api()
    if not api:
        return jsonify({'error': 'Koneksi ke router gagal.'})

    try:
        ping_api = api.get_binary_resource('/')
        raw = api.get_resource('/').call('ping', {'address': host, 'count': count})

        results = []
        sent = received = 0
        rtt_total = 0

        for r in raw:
            sent += 1
            status_raw = r.get('status', '')
            
            # Kalau status kosong = reply berhasil
            is_reply = status_raw == '' or status_raw not in ('timeout', 'no reply')
            status   = 'reply' if is_reply else 'timeout'
            ttl      = r.get('ttl', None)
            rtt      = r.get('time', None)

            if is_reply and rtt:
                received += 1
                # Ambil bagian ms saja, contoh: "65ms458us" → "65"
                import re
                ms_match = re.search(r'(\d+)ms', rtt)
                rtt_ms   = ms_match.group(1) if ms_match else '0'
                rtt_total += int(rtt_ms)
            else:
                rtt_ms = None

            results.append({'status': status, 'rtt': rtt_ms, 'ttl': ttl})

        loss    = round((sent - received) / sent * 100) if sent else 100
        avg_rtt = round(rtt_total / received) if received else 0

        return jsonify({
            'host':        host,
            'packet_loss': f'{loss}%',
            'avg_rtt':     avg_rtt,
            'results':     results
        })

    except Exception as e:
        return jsonify({'error': str(e)})
    finally:
        connection.disconnect()


@app.route('/uji-koneksi/traceroute', methods=['POST'])
@login_required
@router_connected_required
def uji_traceroute():
    host = request.form.get('host', '8.8.8.8')

    api, connection = get_mikrotik_api()
    if not api:
        return jsonify({'error': 'Koneksi ke router gagal.'})

    try:
        tool_api = api.get_binary_resource('/')
        raw = api.get_resource('/').call('tool/traceroute', {'address': host, 'count': '1'})

        results = []
        for r in raw:
            hop     = r.get('#', r.get('hop', '?'))
            address = r.get('address', None)
            rtt     = r.get('time1', r.get('time', None))
            status  = r.get('status', '')

            rtt_ms = ''.join(filter(str.isdigit, rtt)) if rtt else None

            results.append({
                'hop':     hop,
                'address': address,
                'rtt':     rtt_ms,
                'status':  'reached' if address and address == host else status
            })

        return jsonify({'host': host, 'results': results})

    except Exception as e:
        return jsonify({'error': str(e)})
    finally:
        connection.disconnect()


@app.route('/uji-koneksi/interface', methods=['POST'])
@login_required
@router_connected_required
def uji_interface():
    api, connection = get_mikrotik_api()
    if not api:
        return jsonify({'error': 'Koneksi ke router gagal.'})

    try:
        raw = api.get_resource('/interface').get()

        results = [{
            'name':     iface.get('name', '-'),
            'type':     iface.get('type', '-'),
            'running':  iface.get('running', 'false'),
            'disabled': iface.get('disabled', 'true'),
            'comment':  iface.get('comment', '')
        } for iface in raw]

        return jsonify({'results': results})

    except Exception as e:
        return jsonify({'error': str(e)})
    finally:
        connection.disconnect()


@app.route('/uji-koneksi/internet', methods=['POST'])
@login_required
@router_connected_required
def uji_internet():
    hosts = ['8.8.8.8', '1.1.1.1', 'google.com']

    api, connection = get_mikrotik_api()
    if not api:
        return jsonify({'error': 'Koneksi ke router gagal.'})

    try:
        results = []
        for host in hosts:
            try:
                raw = api.get_resource('/').call('ping', {'address': host, 'count': '3'})

                received  = 0
                rtt_total = 0

                for r in raw:
                    status_raw = r.get('status', '')
                    is_reply   = status_raw == '' or status_raw not in ('timeout', 'no reply')
                    if is_reply:
                        received += 1
                        rtt = r.get('time', '0')
                        ms_match   = re.search(r'(\d+)ms', rtt)
                        rtt_ms     = int(ms_match.group(1)) if ms_match else 0
                        rtt_total += rtt_ms

                results.append({
                    'host':      host,
                    'reachable': received > 0,
                    'avg_rtt':   round(rtt_total / received) if received else None
                })

            except Exception:
                results.append({'host': host, 'reachable': False, 'avg_rtt': None})

        return jsonify({'results': results})

    except Exception as e:
        return jsonify({'error': str(e)})
    finally:
        connection.disconnect()

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000, debug=True)