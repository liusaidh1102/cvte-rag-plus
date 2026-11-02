import os
from pathlib import Path

try:
    import valkey as redis
except ImportError:  # management-only environment keeps using redis-py
    import redis
from dotenv import load_dotenv
from elasticsearch import Elasticsearch
from minio import Minio
from pymysql.err import MySQLError
from root_path import get_root_folder

from api.db.db_models import DB

DatabaseError = MySQLError

# 加载环境变量
env_path = Path(get_root_folder()) / "docker" / ".env"
load_dotenv(env_path)


# 检测是否在Docker容器中运行
def is_running_in_docker():
    # 检查是否存在/.dockerenv文件
    docker_env = os.path.exists("/.dockerenv")
    # 或者检查cgroup中是否包含docker字符串
    try:
        with open("/proc/self/cgroup", "r") as f:
            return docker_env or "docker" in f.read()
    except:  # noqa: E722
        return docker_env


# 根据运行环境选择合适的主机地址和端口
if is_running_in_docker():
    MYSQL_HOST = "mysql"
    MYSQL_PORT = 3306
    MINIO_HOST = "minio"
    MINIO_PORT = 9000
    ES_HOST = "es01"
    ES_PORT = 9200
    REDIS_HOST = "redis"
    REDIS_PORT = 6379
else:
    MYSQL_HOST = "localhost"
    MYSQL_PORT = int(os.getenv("MYSQL_PORT", "5455"))
    MINIO_HOST = "localhost"
    MINIO_PORT = int(os.getenv("MINIO_PORT", "9000"))
    ES_HOST = "localhost"
    ES_PORT = int(os.getenv("ES_PORT", "9200"))
    REDIS_HOST = "localhost"
    REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))


# 数据库连接配置
DB_CONFIG = {
    "host": MYSQL_HOST,
    "port": MYSQL_PORT,
    "user": "root",
    "password": os.getenv("MYSQL_PASSWORD", "change_me"),
    "database": os.getenv("MYSQL_DBNAME", "cvte_rag_plus"),
}

# MinIO连接配置
MINIO_CONFIG = {
    "endpoint": f"{MINIO_HOST}:{MINIO_PORT}",
    "access_key": os.getenv("MINIO_USER", "cvte_rag_plus"),
    "secret_key": os.getenv("MINIO_PASSWORD", "change_me"),
    "secure": False,
}

# Elasticsearch连接配置
ES_CONFIG = {
    "host": f"http://{ES_HOST}:{ES_PORT}",
    "user": os.getenv("ELASTIC_USER", "elastic"),
    "password": os.getenv("ELASTIC_PASSWORD", "change_me"),
    "use_ssl": os.getenv("ES_USE_SSL", "false").lower() == "true",
}

# Redis连接配置
REDIS_CONFIG = {
    "host": REDIS_HOST,
    "port": REDIS_PORT,
    "password": os.getenv("REDIS_PASSWORD", "change_me"),
    "decode_responses": False,
}


class PeeweeConnection:
    """DB-API facade over Peewee's pooled MySQL connection.

    Management services historically used mysql-connector cursors directly.
    Keeping this small facade lets those parameterised SQL queries run on the
    same Peewee pool and connection settings as the main API, while preserving
    their existing cursor/commit/rollback call pattern.
    """

    def __init__(self):
        self._connection = DB._connect()
        self._closed = False

    def cursor(self, dictionary=False):
        if dictionary:
            from pymysql.cursors import DictCursor

            return self._connection.cursor(DictCursor)
        return self._connection.cursor()

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def ping(self, reconnect=True):
        return self._connection.ping(reconnect=reconnect)

    def is_connected(self):
        return bool(getattr(self._connection, "open", False))

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            # Never return an uncommitted transaction to the pool.
            self._connection.rollback()
        except Exception:
            pass
        DB._close(self._connection)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def get_db_connection():
    """Check out a connection from Peewee's shared MySQL pool."""
    try:
        return PeeweeConnection()
    except Exception as e:
        print(f"MySQL连接失败: {str(e)}")
        raise e


def get_minio_client():
    """创建MinIO客户端连接"""
    try:
        minio_client = Minio(endpoint=MINIO_CONFIG["endpoint"], access_key=MINIO_CONFIG["access_key"], secret_key=MINIO_CONFIG["secret_key"], secure=MINIO_CONFIG["secure"])
        return minio_client
    except Exception as e:
        print(f"MinIO连接失败: {str(e)}")
        raise e


def get_es_client():
    """创建Elasticsearch客户端连接"""
    try:
        # 构建连接参数
        es_params = {"hosts": [ES_CONFIG["host"]]}

        # 添加认证信息
        if ES_CONFIG["user"] and ES_CONFIG["password"]:
            es_params["basic_auth"] = (ES_CONFIG["user"], ES_CONFIG["password"])

        # 添加SSL配置
        if ES_CONFIG["use_ssl"]:
            es_params["use_ssl"] = True
            es_params["verify_certs"] = False  # 在开发环境中可以设置为False，生产环境应该设置为True

        es_client = Elasticsearch(**es_params)
        return es_client
    except Exception as e:
        print(f"Elasticsearch连接失败: {str(e)}")
        raise e


def get_redis_connection():
    """创建Redis连接"""
    try:
        # 使用配置创建Redis连接
        r = redis.Redis(**REDIS_CONFIG)
        # 测试连接
        r.ping()
        return r
    except Exception as e:
        print(f"Redis连接失败: {str(e)}")
        raise e


def test_connections():
    """测试数据库和MinIO连接"""
    try:
        # 测试MySQL连接
        db_conn = get_db_connection()
        cursor = db_conn.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        cursor.close()
        db_conn.close()
        print("MySQL连接测试成功")

        # 测试MinIO连接
        minio_client = get_minio_client()
        buckets = minio_client.list_buckets()
        print(f"MinIO连接测试成功，共有 {len(buckets)} 个存储桶")

        # 测试Elasticsearch连接
        try:
            es_client = get_es_client()
            es_info = es_client.info()
            print(f"Elasticsearch连接测试成功，版本: {es_info.get('version', {}).get('number', '未知')}")
        except Exception as e:
            print(f"Elasticsearch连接测试失败: {str(e)}")

        return True
    except Exception as e:
        print(f"连接测试失败: {str(e)}")
        return False


if __name__ == "__main__":
    # 如果直接运行此文件，则测试连接
    test_connections()
