import re

class ASCIIUsernameValidator:
    regex = r"\A[\w.@+-]+\Z"
    message = "Enter a valid username."

    def __call__(self, value):
        if not re.match(self.regex, value):
            raise ValueError(self.message)

class UnicodeUsernameValidator:
    regex = r"\A[\w.@+-]+\Z"
    message = "Enter a valid unicode username."

    def __call__(self, value):
        if not re.match(self.regex, value):
            raise ValueError(self.message)
