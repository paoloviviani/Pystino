"""Synthetic Italian/English medical-letter sentences, hand-labelled.

Markup: [[TYPE:text]]. Every name, address, number and organisation is invented.
Gold types: PERSON, LOCATION, ORGANIZATION, DATE_TIME, IT_FISCAL_CODE,
HEALTH_INSURANCE_ID, MEDICAL_CONDITION, MEDICATION.
"""
import re

RAW = [
    "Gentile collega, Le scrivo in merito alla paziente [[PERSON:Maria Rossi]], nata a [[LOCATION:Torino]] il [[DATE_TIME:12/03/1957]].",
    "Il paziente [[PERSON:Giuseppe De Luca]] (codice fiscale [[IT_FISCAL_CODE:RSSMRA80A01H501U]]) è stato ricoverato presso l'[[ORGANIZATION:Ospedale San Camillo]] di [[LOCATION:Roma]].",
    "La [[PERSON:Dott.ssa Francesca Di Marco]], dirigente medico del reparto di [[ORGANIZATION:Cardiologia]], ha visitato la paziente il [[DATE_TIME:3 novembre 2025]].",
    "Residente in [[LOCATION:Via Giuseppe Garibaldi 45, 10121 Torino]], telefono 011 5551234.",
    "Numero di tessera sanitaria: [[HEALTH_INSURANCE_ID:80380000123456789012]], assistito dalla [[ORGANIZATION:ASL Città di Torino]].",
    "Diagnosi: [[MEDICAL_CONDITION:ipertensione arteriosa]] e [[MEDICAL_CONDITION:diabete mellito di tipo 2]] in terapia.",
    "Si prescrive [[MEDICATION:metformina]] 850 mg due volte al giorno e [[MEDICATION:ramipril]] 5 mg alla sera.",
    "Il sig. [[PERSON:Luca Bianchi Ferrari]] riferisce dolore toracico da tre giorni.",
    "Riferisce inoltre episodi di [[MEDICAL_CONDITION:fibrillazione atriale parossistica]] già nota dal 2019.",
    "Si consiglia visita di controllo presso l'[[ORGANIZATION:Azienda Ospedaliero-Universitaria Careggi]] di [[LOCATION:Firenze]].",
    "La signora [[PERSON:Anna Maria Da Ponte]], nata il [[DATE_TIME:15/04/1962]], codice fiscale [[IT_FISCAL_CODE:DVRGPP62D15L219G]].",
    "Inizia terapia con [[MEDICATION:atorvastatina]] 20 mg e [[MEDICATION:acido acetilsalicilico]] 100 mg al giorno.",
    "Eseguito ECG: ritmo sinusale. Non si evidenziano segni di [[MEDICAL_CONDITION:scompenso cardiaco]].",
    "Il [[PERSON:Prof. Alessandro Colombo]] dell'[[ORGANIZATION:Ospedale Niguarda]] di [[LOCATION:Milano]] conferma la diagnosi di [[MEDICAL_CONDITION:carcinoma mammario]].",
    "Dimessa il [[DATE_TIME:21/11/2025]] con diagnosi di [[MEDICAL_CONDITION:polmonite lobare]]; prosegue [[MEDICATION:amoxicillina]] 1 g ogni 8 ore.",
    "Paziente [[PERSON:Chiara Pellegrini]], domiciliata in [[LOCATION:Corso Vittorio Emanuele II, 120, Napoli]].",
    "Contattare il medico di base [[PERSON:Dott. Roberto D'Angelo]] presso lo studio di [[LOCATION:Via Roma 8, Bologna]].",
    "Il paziente è seguito dall'[[ORGANIZATION:ASL Roma 1]] e dall'ambulatorio di [[ORGANIZATION:Diabetologia]] dell'[[ORGANIZATION:Policlinico Gemelli]].",
    "Tessera sanitaria n. [[HEALTH_INSURANCE_ID:80380001234567890123]] intestata a [[PERSON:Marta Ferrari]], codice fiscale [[IT_FISCAL_CODE:FRRMRT90C60A944O]].",
    "Anamnesi: [[MEDICAL_CONDITION:asma bronchiale]] dall'infanzia, allergia alla [[MEDICATION:penicillina]].",
    "Si somministra [[MEDICATION:salbutamolo]] spray al bisogno e [[MEDICATION:budesonide]] 200 mcg due volte al giorno.",
    "Ecografia addominale: nulla da segnalare. Si rimanda al controllo tra sei mesi.",
    "Referto firmato dal [[PERSON:Dott. Stefano Costa]], Radiologia, [[ORGANIZATION:Ospedale Maggiore]] di [[LOCATION:Parma]].",
    "Dear colleague, I am writing about [[PERSON:Mr John Smith]], born on [[DATE_TIME:14 May 1971]], who lives at [[LOCATION:22 Baker Street, London]].",
    "He was diagnosed with [[MEDICAL_CONDITION:chronic kidney disease]] and started on [[MEDICATION:furosemide]] 40 mg daily.",
    "Seen by [[PERSON:Dr. Emily Carter]] at [[ORGANIZATION:St Mary's Hospital]] on [[DATE_TIME:2 October 2025]].",
    "La paziente [[PERSON:Giovanna Pellizzari]] nata a [[LOCATION:Cagliari]], C.F. [[IT_FISCAL_CODE:PLLNNA88S45D612B]], viene inviata in [[ORGANIZATION:Neurologia]] per [[MEDICAL_CONDITION:cefalea cronica]].",
    "Riferisce che il marito, [[PERSON:Antonio Lo Presti]], assume [[MEDICATION:warfarin]] per [[MEDICAL_CONDITION:trombosi venosa profonda]].",
    "Distinti saluti. Il medico curante, [[PERSON:Dott.ssa Elena Moretti]], [[ORGANIZATION:ASL Napoli 2 Nord]].",
    "Esame obiettivo nella norma. Pressione arteriosa 130/80 mmHg. Peso stabile. Consigliata dieta ipocalorica.",
]

TAG = re.compile(r"\[\[([A-Z_]+):(.*?)\]\]")


def parse(raw: str):
    """Return (clean text, [(start, end, type)])."""
    out, spans, last, pos = [], [], 0, 0
    for m in TAG.finditer(raw):
        out.append(raw[last:m.start()])
        pos += m.start() - last
        text = m.group(2)
        spans.append((pos, pos + len(text), m.group(1)))
        out.append(text)
        pos += len(text)
        last = m.end()
    out.append(raw[last:])
    return "".join(out), spans


def load():
    return [parse(r) for r in RAW]


if __name__ == "__main__":
    data = load()
    print(len(data), "sentences", sum(len(s) for _, s in data), "spans")
    for t, s in data[:2]:
        print(t, [(t[a:b], ty) for a, b, ty in s])
