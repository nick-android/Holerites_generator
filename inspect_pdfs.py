import os, pdfplumber
files=['Funcionários y sueldos.pdf','Book.pdf']
for name in files:
    print('\n===', name, '===')
    path=os.path.join(os.getcwd(), name)
    with pdfplumber.open(path) as pdf:
        text=''
        for i,page in enumerate(pdf.pages[:6],1):
            t=page.extract_text() or ''
            text += f'--- PAGE {i} ---\n{t}\n'
        print(text[:25000])
