import pdfplumber
import re

# Analyze Book.pdf
with pdfplumber.open("Book.pdf") as pdf:
    full_text = ""
    for page in pdf.pages:
        text = page.extract_text()
        if text:
            full_text += text + "\n"
    
    # Find lines with dates (biometric data)
    print("BIOMETRIC DATA SAMPLE (first 2000 chars):")
    print("-" * 80)
    print(full_text[:2000])
    
    print("\n\nFIRST 30 LINES:")
    print("-" * 80)
    for i, line in enumerate(full_text.split('\n')[:30]):
        if line.strip():
            print(f"{i}: {line}")
            
    # Count entries with dates
    date_pattern = r'\d{2}/\d{2}/\d{4}'
    dates = re.findall(date_pattern, full_text)
    print(f"\n\nTotal date entries found: {len(dates)}")
    print(f"Date range: {dates[0] if dates else 'None'} to {dates[-1] if dates else 'None'}")
