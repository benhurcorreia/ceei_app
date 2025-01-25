from flask import Flask, request, render_template, jsonify, send_file
import pandas as pd
import os
import threading
import requests
from bs4 import BeautifulSoup
from flask_socketio import SocketIO, emit
import zipfile

app = Flask(__name__)
socketio = SocketIO(app)
UPLOAD_FOLDER = 'uploads'
DOWNLOAD_FOLDER = 'downloads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

results = []
stop_flag = threading.Event()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    file = request.files['file']
    source = request.form.get('source')
    if file:
        file_path = os.path.join(UPLOAD_FOLDER, file.filename)
        file.save(file_path)
        # Start the download process in a separate thread
        stop_flag.clear()
        threading.Thread(target=process_file, args=(file_path, source)).start()
        return jsonify({'message': 'Arquivo enviado e processamento iniciado!'})
    return jsonify({'error': 'Nenhum arquivo enviado.'}), 400

@app.route('/stop', methods=['POST'])
def stop_processing():
    stop_flag.set()
    return jsonify({'message': 'Processamento interrompido com sucesso!'})

@app.route('/download', methods=['GET'])
def download_zip():
    zip_path = os.path.join(DOWNLOAD_FOLDER, 'artigos.zip')
    with zipfile.ZipFile(zip_path, 'w') as zipf:
        for root, _, files in os.walk(DOWNLOAD_FOLDER):
            for file in files:
                if file != 'artigos.zip':  # Avoid adding the zip file itself
                    zipf.write(os.path.join(root, file), arcname=file)
    return send_file(zip_path, as_attachment=True)

def process_file(file_path, source):
    global results
    results = []
    df = pd.read_excel(file_path)
    if 'DOI' not in df.columns:
        results.append({'DOI': 'N/A', 'Status': "Erro: Coluna 'DOI' não encontrada", 'Fonte': source})
        socketio.emit('log', {'message': "Erro: Coluna 'DOI' não encontrada no arquivo."})
        return

    for index, doi in enumerate(df['DOI']):
        if stop_flag.is_set():
            results.append({'DOI': doi, 'Status': "Interrompido pelo usuário", 'Fonte': source})
            socketio.emit('log', {'message': f"Processamento interrompido no DOI: {doi}"})
            break

        save_path = os.path.join(DOWNLOAD_FOLDER, f"{doi.replace('/', '_')}.pdf")
        socketio.emit('log', {'message': f"Processando DOI {index + 1}/{len(df)}: {doi}"})
        if source == 'unpaywall':
            download_from_unpaywall(doi, save_path)
        elif source == 'scihub':
            download_from_scihub(doi, save_path)

        # Emit progress updates to the client
        socketio.emit('progress', {'current': index + 1, 'total': len(df)})

    generate_report()

def download_from_unpaywall(doi, save_path):
    base_url = "https://api.unpaywall.org/v2/"
    params = {"email": "correia.benhur@gmail.com"}
    url = f"{base_url}{doi.strip()}"

    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()

        if data.get('is_oa'):
            best_oa_location = data.get('best_oa_location')
            if best_oa_location and best_oa_location.get('url_for_pdf'):
                pdf_url = best_oa_location['url_for_pdf']
                download_pdf(pdf_url, save_path, doi, 'Unpaywall')
            else:
                results.append({'DOI': doi, 'Status': "Erro: Nenhum PDF disponível", 'Fonte': 'Unpaywall'})
                socketio.emit('log', {'message': f"Erro: Nenhum PDF disponível para DOI {doi}"})
        else:
            results.append({'DOI': doi, 'Status': "Erro: Não disponível em acesso aberto", 'Fonte': 'Unpaywall'})
            socketio.emit('log', {'message': f"Erro: Artigo não disponível em acesso aberto para DOI {doi}"})
    except Exception as e:
        results.append({'DOI': doi, 'Status': f"Erro: {e}", 'Fonte': 'Unpaywall'})
        socketio.emit('log', {'message': f"Erro ao buscar DOI {doi}: {e}"})

def download_from_scihub(doi, save_path):
    sci_hub_base_url = "https://sci-hub.se"
    try:
        response = requests.post(sci_hub_base_url, data={'request': doi}, timeout=20)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')

        pdf_link = None
        iframe = soup.find('iframe')
        if iframe:
            pdf_link = iframe['src']

        if not pdf_link:
            embed = soup.find('embed', type="application/pdf")
            if embed:
                pdf_link = embed['src']

        if pdf_link and pdf_link.startswith('/'):
            pdf_link = sci_hub_base_url + pdf_link
        elif pdf_link and not pdf_link.startswith('http'):
            pdf_link = 'https://' + pdf_link

        if pdf_link:
            download_pdf(pdf_link, save_path, doi, 'Sci-Hub')
        else:
            results.append({'DOI': doi, 'Status': "Erro: Nenhum PDF encontrado", 'Fonte': 'Sci-Hub'})
            socketio.emit('log', {'message': f"Erro: Nenhum PDF encontrado para DOI {doi}"})
    except Exception as e:
        results.append({'DOI': doi, 'Status': f"Erro: {e}", 'Fonte': 'Sci-Hub'})
        socketio.emit('log', {'message': f"Erro ao buscar DOI {doi} no Sci-Hub: {e}"})

def download_pdf(pdf_url, save_path, doi, source):
    try:
        response = requests.get(pdf_url, timeout=30)
        response.raise_for_status()
        with open(save_path, 'wb') as f:
            f.write(response.content)
        results.append({'DOI': doi, 'Status': 'Baixado', 'Fonte': source})
        socketio.emit('log', {'message': f"PDF baixado com sucesso: DOI {doi}"})
    except Exception as e:
        results.append({'DOI': doi, 'Status': f"Erro ao baixar PDF: {e}", 'Fonte': source})
        socketio.emit('log', {'message': f"Erro ao baixar PDF para DOI {doi}: {e}"})

def generate_report():
    report_path = os.path.join(DOWNLOAD_FOLDER, "relatorio_download.xlsx")
    df = pd.DataFrame(results)
    df.to_excel(report_path, index=False, engine='openpyxl')
    socketio.emit('log', {'message': f"Relatório de download salvo em: {report_path}"})

if __name__ == '__main__':
    socketio.run(app, debug=True, allow_unsafe_werkzeug=True)