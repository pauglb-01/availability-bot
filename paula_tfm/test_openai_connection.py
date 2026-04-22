from connect.openai_client import chat_completion

response = chat_completion(
    [{"role": "user", "content": "Say hello in Spanish in one sentence"}]
)

print("OpenAI response:")
print(response)
