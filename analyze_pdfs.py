import pdfplumber
import json

def analyze_pdf(filepath, name):
    print(f"\n{'='*80}")
    print(f"ANALYZING: {name}")
    print(f"{'='*80}\n")
    try:
        with pdfplumber.open(filepath) as pdf:
            print(f"Total Pages: {len(pdf.pages)}\n")
            
            # Extract all text
            full_text = ""
            for page_num, page in enumerate(pdf.pages):
                text = page.extract_text()
                if text:
                    full_text += text + "\n"
            
            # Print first 3000 characters to see structure
            print("FULL TEXT (First 3000 chars):")
            print("-" * 80)
            print(full_text[:3000])
            print("\n" + "-" * 80)
            
            # Look for lines with PYG (salary entries)
            print("\nLINES WITH 'PYG' (Employee entries):")
            print("-" * 80)
            lines_with_pyg = []
            for i, line in enumerate(full_text.split('\n')):
                if 'PYG' in line.upper():
                    lines_with_pyg.append(line)
                    print(f"{i}: {line}")
            
            if not lines_with_pyg:
                print("NO LINES WITH 'PYG' FOUND!")
                print("\nShowing ALL lines (first 50):")
                for i, line in enumerate(full_text.split('\n')[:50]):
                    if line.strip():
                        print(f"{i}: {line}")
            
            # Look for "Ignacio Venialgo"
            print("\n" + "-" * 80)
            print("SEARCHING FOR 'IGNACIO VENIALGO':")
            print("-" * 80)
            if 'IGNACIO' in full_text.upper() or 'VENIALGO' in full_text.upper():
                for i, line in enumerate(full_text.split('\n')):
                    if 'IGNACIO' in line.upper() or 'VENIALGO' in line.upper():
                        # Print context: 2 lines before and after
                        lines = full_text.split('\n')
                        start = max(0, i-2)
                        end = min(len(lines), i+3)
                        for j in range(start, end):
                            marker = ">>> " if j == i else "    "
                            print(f"{marker}{j}: {lines[j]}")
                        print()
            else:
                print("NOT FOUND in document!")
                
    except Exception as e:
        print(f"ERROR: {e}")

# Analyze both documents
analyze_pdf("Planilla de Funcionarios y Sueldos - Planilla de Funcionarios y Sueldos.pdf", 
            "PLANILLA DE FUNCIONARIOS Y SUELDOS")
analyze_pdf("Book.pdf", "BOOK (BIOMETRIC DATA)")
