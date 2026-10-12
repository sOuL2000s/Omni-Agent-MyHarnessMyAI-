
name = input("Enter your name: ")
birth_year_str = input("Enter your birth year: ")

try:
    birth_year = int(birth_year_str)
    import datetime
    current_year = datetime.datetime.now().year
    age = current_year - birth_year

    print(f"Hello, {name}! You are {age} years old.")
except ValueError:
    print("Invalid birth year. Please enter a number.")
