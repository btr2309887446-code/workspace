import sys

def solve():
    # Read all inputs from standard input
    input_data = sys.stdin.read().split()
    
    first = True
    for year_str in input_data:
        if not year_str:
            continue
            
        year = int(year_str)
        
        # Determine properties
        is_leap = (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0)
        is_hulu = (year % 15 == 0)
        is_bulu = is_leap and (year % 55 == 0)
        
        # A blank line should separate the output for each line of input
        if not first:
            print("")
        first = False
        
        ordinary = True
        
        if is_leap:
            print("This is leap year.")
            ordinary = False
        if is_hulu:
            print("This is huluculu festival year.")
            ordinary = False
        if is_bulu:
            print("This is bulukulu festival year.")
            ordinary = False
            
        if ordinary:
            print("This is an ordinary year.")

if __name__ == '__main__':
    solve()
