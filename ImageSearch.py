# %%pip install -U ftfy transformers sentencepiece gradio huggingface_hub accelerate protobuf oracledb pillow python-dotenv
import os
import io
from typing import Union, List
import ftfy, html, re
import torch
import gradio as gr
from PIL import Image
from transformers import AutoModel, AutoTokenizer, AutoImageProcessor, BatchFeature
import oracledb
from dotenv import load_dotenv, find_dotenv
import json

_ = load_dotenv(find_dotenv())

# データベース接続情報
username = os.getenv("DB_USER")
password = os.getenv("DB_PASSWORD")
dsn = os.getenv("DB_DSN")

# Japanese Stable CLIPモデルのロード
device = "cuda" if torch.cuda.is_available() else "cpu"
model_path = "stabilityai/japanese-stable-clip-vit-l-16"
model = AutoModel.from_pretrained(model_path, trust_remote_code=True).eval().to(device)
tokenizer = AutoTokenizer.from_pretrained(model_path)
processor = AutoImageProcessor.from_pretrained(model_path)

def basic_clean(text):
    text = ftfy.fix_text(text)
    text = html.unescape(html.unescape(text))
    return text.strip()


def whitespace_clean(text):
    text = re.sub(r"\s+", " ", text)
    text = text.strip()
    return text

def tokenize(
    texts: Union[str, List[str]],
    max_seq_len: int = 77,
):
    if isinstance(texts, str):
        texts = [texts]
    texts = [whitespace_clean(basic_clean(text)) for text in texts]

    inputs = tokenizer(
        texts,
        max_length=max_seq_len - 1,
        padding="max_length",
        truncation=True,
        add_special_tokens=False,
    )
    # add bos token at first place
    input_ids = [[tokenizer.bos_token_id] + ids for ids in inputs["input_ids"]]
    attention_mask = [[1] + am for am in inputs["attention_mask"]]
    position_ids = [list(range(0, len(input_ids[0])))] * len(texts)

    return BatchFeature(
        {
            "input_ids": torch.tensor(input_ids, dtype=torch.long),
            "attention_mask": torch.tensor(attention_mask, dtype=torch.long),
            "position_ids": torch.tensor(position_ids, dtype=torch.long),
        }
    )

def compute_text_embeddings(text):
  if isinstance(text, str):
    text = [text]
  text = tokenize(texts=text)
  text_features = model.get_text_features(**text.to(device))
  text_features = text_features / text_features.norm(p=2, dim=-1, keepdim=True)
  del text
  return text_features.cpu().detach()

def compute_image_embeddings(image):
  image = processor(images=image, return_tensors="pt").to(device)
  with torch.no_grad():
    image_features = model.get_image_features(**image)
  image_features = image_features / image_features.norm(p=2, dim=-1, keepdim=True)
  del image
  return image_features.cpu().detach()

def get_latest_images(limit=16):
    connection = oracledb.connect(user=username, password=password, dsn=dsn)
    cursor = connection.cursor()

    cursor.execute("""
        SELECT i.image_id, i.file_name, i.generation_prompt, d.description
        FROM IMAGES i
        LEFT JOIN IMAGE_DESCRIPTIONS d ON i.image_id = d.image_id
        ORDER BY i.upload_date DESC
        FETCH FIRST :limit ROWS ONLY
    """, {'limit': limit})

    results = cursor.fetchall()
    
    # LOBオブジェクトを文字列に変換
    processed_results = []
    for row in results:
        image_id, file_name, generation_prompt, description = row
        processed_results.append((
            image_id,
            file_name,
            generation_prompt.read() if generation_prompt else None,
            description.read() if description else None
        ))

    cursor.close()
    connection.close()

    return processed_results

def load_initial_images():
    results = get_latest_images(limit=16)
    images = []
    image_info = []
    for index, (image_id, file_name, generation_prompt, description) in enumerate(results):
        image_data = get_image_data(image_id)
        images.append(Image.open(io.BytesIO(image_data)))
        image_info.append({
            'file_name': file_name,
            'generation_prompt': generation_prompt,
            'caption': description
        })
        #print("-----------------------------------------")
        #print(f"image_id: {image_id}")
        #print(f"image_info[{index}]:\n{image_info[index]}")
        #print("-----------------------------------------")
    return images, image_info

def search_images(query, search_type, limit=16):
    connection = oracledb.connect(user=username, password=password, dsn=dsn)
    cursor = connection.cursor()

    if search_type == "text":
        embedding_json = json.dumps(compute_text_embeddings(query).tolist()[0])
        cursor.execute("""
            SELECT i.image_id, i.file_name, i.generation_prompt, d.description,
                   cie.embedding <#> :query_embedding as similarity
            FROM CURRENT_IMAGE_EMBEDDINGS cie
            JOIN IMAGES i ON cie.image_id = i.image_id
            LEFT JOIN IMAGE_DESCRIPTIONS d ON i.image_id = d.image_id
            ORDER BY similarity
            FETCH FIRST :limit ROWS ONLY
        """, {'query_embedding': embedding_json, 'limit': limit})
    elif search_type == "image":
        embedding_json = json.dumps(compute_image_embeddings(query).tolist()[0])
        cursor.execute("""
            SELECT i.image_id, i.file_name, i.generation_prompt, d.description,
                   cie.embedding <#> :query_embedding as similarity
            FROM CURRENT_IMAGE_EMBEDDINGS cie
            JOIN IMAGES i ON cie.image_id = i.image_id
            LEFT JOIN IMAGE_DESCRIPTIONS d ON i.image_id = d.image_id
            ORDER BY similarity
            FETCH FIRST :limit ROWS ONLY
        """, {'query_embedding': embedding_json, 'limit': limit})
    else:
        raise ValueError("Invalid search type")

    results = cursor.fetchall()
    
    # LOBオブジェクトを文字列に変換
    processed_results = []
    for row in results:
        image_id, file_name, generation_prompt, description, similarity = row
        processed_results.append((
            image_id,
            file_name,
            generation_prompt.read() if generation_prompt else None,
            description.read() if description else None,
            similarity
        ))

    cursor.close()
    connection.close()

    return processed_results

def get_image_data(image_id):
    connection = oracledb.connect(user=username, password=password, dsn=dsn)
    cursor = connection.cursor()

    cursor.execute("SELECT image_data FROM IMAGES WHERE image_id = :image_id", {'image_id': image_id})
    image_data = cursor.fetchone()[0].read()

    cursor.close()
    connection.close()

    return image_data

def search(query, search_type, page=1):
    results = search_images(query, search_type, limit=16)
    images = []
    image_info = []
    for index, (image_id, file_name, generation_prompt, description, similarity) in enumerate(results):
        image_data = get_image_data(image_id)
        images.append(Image.open(io.BytesIO(image_data)))
        image_info.append({
            'file_name': file_name,
            'generation_prompt': generation_prompt,
            'caption': description,
            'similarity': similarity
        })
        #print("-----------------------------------------")
        #print(f"image_id: {image_id}")
        #print(f"image_info[{index}]:\n{image_info[index]}")
        #print("-----------------------------------------")
    return images, image_info

def on_select(evt: gr.SelectData, image_info):
    selected_index = evt.index
    if 0 <= selected_index < len(image_info):
        info = image_info[selected_index]
        similarity = info.get('similarity', 'N/A')
        return info['file_name'], str(similarity), info['generation_prompt'], info['caption']
    else:
        return "選択エラー", "N/A", "選択エラー", "選択エラー"

with gr.Blocks(title="類似画像検索") as demo:
    image_info_state = gr.State([])
    images_state = gr.State([])
    with gr.Row():
        with gr.Column(scale=4):
            gr.Markdown("# 類似画像検索 - Powered by Oracle AI Vector Search with Japanese Stable CLIP")
            text_input = gr.Textbox(label="検索テキスト", lines=4)
            with gr.Row():  
                with gr.Column(scale=2):
                    search_button = gr.Button("検索")
                with gr.Column(scale=1):
                    clear_button = gr.Button("クリア")
        with gr.Column(scale=1):
            image_input = gr.Image(label="検索画像", type="pil", height=280, width=500, interactive=True)
    with gr.Row():    
        with gr.Column(scale=7):
            initial_images, initial_image_info = load_initial_images()
            gallery = gr.Gallery(label="検索結果", show_label=False, elem_id="gallery", columns=[8], rows=[2], height=380, interactive=False, show_download_button=False)
            image_info_state.value = initial_image_info
    
    with gr.Row():
        with gr.Column(scale=1):
            file_name = gr.Textbox(label="ファイル名")
            distance = gr.Textbox(label="ベクトル距離（-1 x 内積）")
        with gr.Column(scale=2):        
            generation_prompt = gr.Textbox(label="画像生成プロンプト", lines=4)
        with gr.Column(scale=2):
            caption = gr.Textbox(label="キャプション", lines=4)

    def search_wrapper(text_query, image_query):
        if text_query:
            images, image_info = search(text_query, "text")
        elif image_query is not None:
            images, image_info = search(image_query, "image")
        else:
            images, image_info = load_initial_images()
        return images, image_info, gr.update(interactive=False), gr.update(interactive=False), gr.update(interactive=False)
        
    def clear_inputs():
        return gr.update(value="", interactive=True), gr.update(value=None, interactive=True), gr.update(interactive=True)

    search_button.click(search_wrapper, inputs=[text_input, image_input], outputs=[gallery, image_info_state, text_input, image_input, search_button])
    clear_button.click(clear_inputs,inputs=[],outputs=[text_input, image_input, search_button])
    gallery.select(on_select, [image_info_state], [file_name, distance, generation_prompt, caption])

    def load_images():
        images, image_info = load_initial_images()
        return images, image_info, images

    demo.load(load_images, outputs=[gallery, image_info_state, images_state])

if __name__ == "__main__":
    try:
        demo.queue()
        demo.launch(inbrowser=True, debug=True, share=True, server_port=8899)
    except KeyboardInterrupt:
        demo.close()
    except Exception as e:
        print(e)
        demo.close()