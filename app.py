from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import os
from dotenv import load_dotenv
from flask_sqlalchemy import SQLAlchemy
import routeros_api
import re

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