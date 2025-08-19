import streamlit as st
from playwright_controller import PlaywrightController
from langchain_openai import ChatOpenAI
from langchain.schema import HumanMessage, SystemMessage
import os

# --- Page Setup ---
st.set_page_config(page_title="Playwright Chatbot", layout="wide")
st.title("Playwright Automation Chatbot")

# --- Sidebar ---
with st.sidebar:
    st.header("Configuration")
    openai_api_key = st.text_input("Enter your OpenAI API Key:", type="password", key="api_key_input")
    if openai_api_key:
        os.environ["OPENAI_API_KEY"] = openai_api_key

    st.markdown("---")
    st.markdown("### Manual Commands")
    st.code("/launch\n/open\n/goto <url>\n/get_page_contents\n/close\n/save <filename.py>\n/shutdown")
    st.markdown("Or, just talk to the bot in natural language!")

# --- Initialization ---
if "controller" not in st.session_state:
    st.session_state.controller = PlaywrightController()

if "messages" not in st.session_state:
    st.session_state.messages = [{"role": "assistant", "content": "Hello! I'm a Playwright chatbot. I can help you automate web browsing. Start by typing `/launch` or `start the server`."}]

controller: PlaywrightController = st.session_state.controller

def get_llm_response(prompt):
    if not os.getenv("OPENAI_API_KEY"):
        return "COMMAND: chat ARGS: Please provide your OpenAI API key in the sidebar to use natural language commands."

    chat = ChatOpenAI(temperature=0)

    system_prompt = """
    You are an assistant that controls a web browser. Your goal is to translate user requests into specific commands.
    The available commands are:
    - `launch`: Starts the browser server.
    - `open`: Opens a new browser window.
    - `goto <url>`: Navigates to a specific URL.
    - `close`: Closes the browser.
    - `save <filename>`: Saves the automation script.
    - `shutdown`: Terminates the entire session.

    Based on the user's prompt, determine the command and its arguments.
    Respond with the command in the format `COMMAND: <command_name> ARGS: <arguments>`.
    If the prompt is a general question or doesn't map to a command, respond with `COMMAND: chat ARGS: <Your friendly response>`.

    Examples:
    User: "start the server" -> COMMAND: launch ARGS: None
    User: "open the browser for me" -> COMMAND: open ARGS: None
    User: "go to google.com" -> COMMAND: goto ARGS: google.com
    User: "can you navigate to https://www.github.com" -> COMMAND: goto ARGS: https://www.github.com
    User: "save the script as test.py" -> COMMAND: save ARGS: test.py
    User: "shut everything down" -> COMMAND: shutdown ARGS: None
    User: "what can you do?" -> COMMAND: chat ARGS: I can control a web browser for you! What would you like to do?
    """

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=prompt)
    ]

    try:
        response = chat.invoke(messages)
        return response.content
    except Exception as e:
        # Handle potential API errors, e.g., invalid key
        if "api_key" in str(e):
            return "COMMAND: chat ARGS: Your OpenAI API key appears to be invalid. Please check it in the sidebar."
        return f"COMMAND: chat ARGS: An error occurred with the AI model: {e}"


def parse_and_execute(llm_output: str, original_prompt: str):
    if "COMMAND:" in llm_output:
        try:
            parts = llm_output.split("ARGS:")
            command_part = parts[0].replace("COMMAND:", "").strip()
            args_part = parts[1].strip() if len(parts) > 1 else None

            if args_part == "None":
                args_part = None

            if command_part == "launch":
                return controller.launch_mcp_server()
            elif command_part == "open":
                return controller.open_browser()
            elif command_part == "goto":
                print(f"DEBUG: Navigating to URL: {args_part}")
                return controller.goto(args_part) if args_part else "Please provide a URL."
            elif command_part == "close":
                return controller.close_browser()
            elif command_part == "save":
                return controller.save_script(args_part) if args_part else "Please provide a filename."
            elif command_part == "get_page_contents":
                return controller.get_page_contents()
            elif command_part == "summarize_page":
                return controller.summarize_page()
            elif command_part == "shutdown":
                res = controller.shutdown()
                st.session_state.messages = [{"role": "assistant", "content": "Session has been shut down. Reload the page to start over."}]
                if "controller" in st.session_state:
                    del st.session_state.controller
                return res
            elif command_part == "chat":
                return args_part if args_part else f"I'm not sure how to handle '{original_prompt}'. Try asking me to 'open a browser' or 'go to a website'."
        except Exception as e:
            return f"Error parsing LLM response: {e}. Response was: '{llm_output}'"

    return f"I'm not sure how to do that. Please try rephrasing your request. (LLM response: {llm_output})"


# --- Chat Interface ---
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

if prompt := st.chat_input("What would you like to do?"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        response = ""
        is_shutdown = False
        with st.spinner("Thinking..."):
            try:
                # Command parsing logic
                if prompt.startswith("/"):
                    parts = prompt.split()
                    command = parts[0].lower()
                    args = parts[1:] if len(parts) > 1 else None

                    # Format as if it came from the LLM to reuse the parser
                    llm_command_str = f"COMMAND: {command.replace('/', '')} ARGS: {' '.join(args) if args else 'None'}"
                    response = parse_and_execute(llm_command_str, prompt)
                else:
                    # Natural language processing
                    llm_response = get_llm_response(prompt)
                    response = parse_and_execute(llm_response, prompt)

                if isinstance(response, str) and "shutdown" in response.lower():
                    is_shutdown = True

            except Exception as e:
                response = f"An unexpected error occurred: {e}"

        st.markdown(response)
        st.session_state.messages.append({"role": "assistant", "content": response})

        if is_shutdown:
            st.stop()
