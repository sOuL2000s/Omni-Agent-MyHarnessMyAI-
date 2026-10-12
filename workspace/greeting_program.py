name = input("Enter your name: ")
age_str = input("Enter your age: ")

try:
    age = int(age_str)
    print(f"Hello, {name}! You are {age} years old.")
except ValueError:
    print("Invalid age. Please enter a number.")
