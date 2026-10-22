import csv
import os

def combine_csv_files(input_files, output_file):
    """
    Combines multiple CSV files into one, keeping only the header of the first file.

    Args:
        input_files (list): List of input CSV file paths.
        output_file (str): Path to the output CSV file.
    """
    if not input_files:
        print("No input files provided.")
        return
    
    with open(output_file, mode='w', newline='', encoding='utf-8') as outfile:
        writer = None
        for idx, file in enumerate(input_files):
            with open(file, mode='r', encoding='utf-8') as infile:
                reader = csv.reader(infile)
                header = next(reader)  # Read the header from the current file
                
                # Initialize the writer with the header from the first file
                if idx == 0:
                    writer = csv.writer(outfile)
                    writer.writerow(header)
                
                # Write the remaining rows
                for row in reader:
                    writer.writerow(row)
    
    print(f"Combined files saved to {output_file}")


def append_csv_files(input_files, output_file):
    """
    Appends the rows of input CSV files to output_file, skipping any row that is
    already in it (or was appended earlier in this run). Each input's header row
    is dropped. If output_file doesn't exist or is empty, it is created with the
    first input file's header.

    Args:
        input_files (list): List of input CSV file paths.
        output_file (str): Path to the CSV file to append to.

    Returns:
        tuple: (rows_added, rows_skipped)
    """
    seen = set()
    has_content = os.path.exists(output_file) and os.path.getsize(output_file) > 0
    if has_content:
        with open(output_file, mode='r', newline='', encoding='utf-8') as existing:
            reader = csv.reader(existing)
            next(reader, None)  # Skip the header
            seen.update(tuple(row) for row in reader)
        # Make sure appended rows start on a new line
        with open(output_file, mode='rb') as existing:
            existing.seek(-1, os.SEEK_END)
            needs_newline = existing.read(1) not in (b'\n', b'\r')
    else:
        needs_newline = False

    added = skipped = 0
    with open(output_file, mode='a', newline='', encoding='utf-8') as outfile:
        if needs_newline:
            outfile.write('\r\n')
        writer = csv.writer(outfile)
        for idx, file in enumerate(input_files):
            with open(file, mode='r', newline='', encoding='utf-8') as infile:
                reader = csv.reader(infile)
                header = next(reader)
                if idx == 0 and not has_content:
                    writer.writerow(header)
                for row in reader:
                    key = tuple(row)
                    if key in seen:
                        skipped += 1
                        continue
                    seen.add(key)
                    writer.writerow(row)
                    added += 1

    print(f"Appended {added} row(s) to {output_file}, skipped {skipped} already present")
    return added, skipped

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Combine multiple CSV files into one.")
    parser.add_argument(
        "input_files", nargs="+", help="Paths to input CSV files to be combined."
    )
    parser.add_argument(
        "--output", required=True, help="Path to the output CSV file."
    )
    parser.add_argument(
        "--append", action="store_true",
        help="Append to the output file instead of overwriting it, skipping rows already in it."
    )

    args = parser.parse_args()

    if any(not os.path.exists(file) for file in args.input_files):
        print("One or more input files do not exist.")
    elif args.append:
        append_csv_files(args.input_files, args.output)
    else:
        combine_csv_files(args.input_files, args.output)
