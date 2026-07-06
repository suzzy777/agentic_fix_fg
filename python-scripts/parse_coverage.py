import sys
import csv
import os
import xml.etree.ElementTree as ET

# Input parameters
method_only = sys.argv[1]
xml_file = sys.argv[2]  # Full path to the XML file
output_csv = 'coverage_results.csv'

def extract_methods_from_xml(xml_file): 
    tree = ET.parse(xml_file)
    root = tree.getroot()
    methods = []
    for pkg in root.findall('package'):
        for cls in pkg.findall('class'):
            xml_class_name = cls.get('name').replace('/', '.')
            for m in cls.findall('method'):
                instr = int(m.find("counter[@type='INSTRUCTION']").get('covered', '0'))
                if instr > 0:
                    methods.append(f"{xml_class_name}.{m.get('name')}")
    return methods

def extract_covered_lines_and_pct(xml_file):
    tree = ET.parse(xml_file)
    root = tree.getroot()
    covered_lines = []
    for pkg in root.findall('package'):
        pkg_name = pkg.get('name').replace('/', '.')
        for src in pkg.findall('sourcefile'):
            src_name = src.get('name')
            for line in src.findall('line'):
                ci = int(line.get('ci', '0'))
                if ci > 0:
                    covered_lines.append(f"{pkg_name}.{src_name}:{line.get('nr')}")
    ctr = root.find("counter[@type='LINE']")
    cov = int(ctr.get('covered','0'))
    miss= int(ctr.get('missed','0'))
    pct = (cov/(cov+miss)*100) if (cov+miss)>0 else 0.0
    return covered_lines, pct, cov

# Extract coverage data
methods = extract_methods_from_xml(xml_file)
covered_lines, pct, cov = extract_covered_lines_and_pct(xml_file)

# Prepare row data                                                                                                                                                    
row = [
    method_only,
    ";".join(methods),
    ";".join(covered_lines),
    f"{pct:.1f}",
    str(cov)
]

# Write to CSV
write_header = not os.path.isfile(output_csv)
with open(output_csv, 'a', newline='') as csvfile:
    writer = csv.writer(csvfile)
    if write_header:
        writer.writerow(['MethodOnly', 'CoveredMethods', 'CoveredLines', 'CoveragePct', 'TotalCoveredLines'])
    writer.writerow(row)

print(f"✅ Coverage data written to {output_csv}")

