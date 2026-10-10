"""
What this teaches:
    Your first call to Gemma 4. You send a question (a "prompt") and get text back.

How to run it:
    python 01_hello.py

What you should see:
    A short poem or sentence about the topic below. It will be different each time you change the prompt.
    Run it twice: the second run is instant, because the answer was saved in .cache/.
"""

import gemma_client  # our helper: it handles your key, retries and saving answers

# The prompt is the question we send to the model. Change it and run again.
prompt = "explain blackhole to 10 year old."

# ask() sends the prompt to Gemma 4 through the Gemini API and returns the answer as text.
answer = gemma_client.ask(prompt)

# Show which model answered, then the answer.
print("Model:", gemma_client.MODEL)
print(answer)
