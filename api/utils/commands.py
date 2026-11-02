#
#  Copyright 2024 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

import base64
import click
import re

from flask import Flask
from werkzeug.security import generate_password_hash

from api.db.services import UserService


@click.command('reset-password', help='Reset the account password.')
@click.option('--email', prompt=True, help='The email address of the account whose password you need to reset')
@click.option('--new-password', prompt=True, help='the new password.')
@click.option('--password-confirm', prompt=True, help='the new password confirm.')
def reset_password(email, new_password, password_confirm):
    if str(new_password).strip() != str(password_confirm).strip():
        click.echo(click.style('sorry. The two passwords do not match.', fg='red'))
        return
    user = UserService.query(email=email)
    if not user:
        click.echo(click.style('sorry. The Email is not registered!.', fg='red'))
        return
    encode_password = base64.b64encode(new_password.encode('utf-8')).decode('utf-8')
    password_hash = generate_password_hash(encode_password)
    user_dict = {
        'password': password_hash
    }
    UserService.update_user(user[0].id,user_dict)
    click.echo(click.style('Congratulations! Password has been reset.', fg='green'))


@click.command('reset-email', help='Reset the account email.')
@click.option('--email', prompt=True, help='The old email address of the account whose email you need to reset')
@click.option('--new-email', prompt=True, help='the new email.')
@click.option('--email-confirm', prompt=True, help='the new email confirm.')
def reset_email(email, new_email, email_confirm):
    if str(new_email).strip() != str(email_confirm).strip():
        click.echo(click.style('Sorry, new email and confirm email do not match.', fg='red'))
        return
    if str(new_email).strip() == str(email).strip():
        click.echo(click.style('Sorry, new email and old email are the same.', fg='red'))
        return
    user = UserService.query(email=email)
    if not user:
        click.echo(click.style('sorry. the account: [{}] not exist .'.format(email), fg='red'))
        return
    if not re.match(r"^[\w\._-]+@([\w_-]+\.)+[\w-]{2,4}$", new_email):
        click.echo(click.style('sorry. {} is not a valid email. '.format(new_email), fg='red'))
        return
    new_user = UserService.query(email=new_email)
    if new_user:
        click.echo(click.style('sorry. the account: [{}] is exist .'.format(new_email), fg='red'))
        return
    user_dict = {
        'email': new_email
    }
    UserService.update_user(user[0].id,user_dict)
    click.echo(click.style('Congratulations!, email has been reset.', fg='green'))

def register_commands(app: Flask):
    app.cli.add_command(reset_password)
    app.cli.add_command(reset_email)
    app.cli.add_command(init_storage)


@click.command("init-storage", help="为知识库桶配置 MinIO 生命周期（清理未完成的 multipart）与 CORS。")
@click.option("--bucket", "buckets", multiple=True, help="知识库 bucket（即 kb_id），可传多次；不传则处理全部 bucket")
@click.option("--cors-origin", "cors_origins", multiple=True, default=["*"], help="允许的前端来源，可传多次")
@click.option("--abort-days", default=1, help="超过 N 天未 complete 的分片自动清理（S3 生命周期最小粒度是天）")
def init_storage(buckets, cors_origins, abort_days):
    """在容器里执行：docker compose exec app flask init-storage

    MinIO 的 AbortIncompleteMultipartUpload 只能按“天”配置，因此 24h 任务 TTL 对应 1 天。
    """
    from rag.utils.minio_multipart import MINIO_MULTIPART

    try:
        target = list(buckets)
        if not target:
            target = [b.name for b in MINIO_MULTIPART.client.list_buckets()]
        if not target:
            click.echo(click.style("no bucket found", fg="yellow"))
            return
        for bucket in target:
            result = MINIO_MULTIPART.configure_bucket(bucket, list(cors_origins), int(abort_days))
            click.echo(f"{bucket}: lifecycle={result['lifecycle']} cors={result['cors']}")
            if str(result["lifecycle"]).startswith("unsupported"):
                click.echo(
                    click.style(
                        "  提示：该 MinIO 会丢弃 AbortIncompleteMultipartUpload 规则，"
                        "未完成分片由服务端扫描清理，请确认 MINIO_API_STALE_UPLOADS_EXPIRY 已生效。",
                        fg="yellow",
                    )
                )
    except Exception as e:
        click.echo(click.style(f"init-storage failed: {e}", fg="red"))
