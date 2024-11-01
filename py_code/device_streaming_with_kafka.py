import json

from pyspark.sql import functions as F
from pyspark.sql import SparkSession
from pyspark.sql.dataframe import DataFrame


def load_config(config_file: str) -> dict:
    with open(config_file) as f:
        return json.load(f)


class Streaming_ETL:
    def __init__(self, config: dict):
        self.spark_session_info = config['spark_session_info']
        self.kafka_information = config['kafka_information']
        self.database_info = config['database_info']

        # Initialize SparkSession
        self.spark = SparkSession.builder \
            .appName("Streaming_Devices_Data") \
            .config("spark.jars", self.spark_session_info['postgres_jars_path']) \
            .config("spark.jars.packages", self.spark_session_info['jars_package']) \
            .getOrCreate()


    def extract(self) -> DataFrame:
        # Read Kafka Stream and extract data schema
        input_df = self.spark.readStream \
            .format("kafka") \
            .option("kafka.bootstrap.servers", self.kafka_information['host']) \
            .option("subscribe", self.kafka_information['topic']) \
            .load()

        return input_df

    def process(self, input_df: DataFrame) -> DataFrame:
        # Select json values to further processing and normalization
        processed_df = input_df.select(
            F.col("key").cast("string").alias("key"),
            F.col("value").cast("string").alias("json_value")
        )

        return processed_df.fillna(0)

    def transform(self, processed_df: DataFrame) -> DataFrame:
        transformed = processed_df.select(
            'key',
            F.expr("get_json_object(json_value, '$.device')").cast('string').alias('device'),
            F.expr("get_json_object(json_value, '$.collected_at')").cast('timestamp').alias('collected_at'),

            # Extract thermal_zones as an array of floats
            F.from_json(
                F.expr("get_json_object(json_value, '$.cpu.thermal_zones')"), 'array<float>'
            ).alias('thermal_zones'),

            # Extract sensors.CPU as a float
            F.expr("get_json_object(json_value, '$.cpu.sensors.CPU')").cast('float').alias('sensors_cpu'),

            # Extract sensors as a map of strings to floats
            F.from_json(
                F.expr("get_json_object(json_value, '$.cpu.sensors')"), 'map<string,float>'
            ).alias('sensors')
        )

        transformed = transformed.withColumn('cpu_value', F.coalesce(
            F.when(
                F.col('thermal_zones').isNotNull(),
                F.expr('aggregate(thermal_zones, cast(0.0 as double), (acc, x) -> acc + x) / size(thermal_zones)')
            ),
            F.when(
                F.col('sensors_cpu').isNotNull(),
                F.col('sensors_cpu')
            ),
            F.when(
                F.col('sensors').isNotNull(),
                F.expr('element_at(map_values(sensors), 1)')
            ),
        ))

        return transformed.select(
            F.col('key'),
            F.col('device'),
            F.col('collected_at'),
            F.col('cpu_value').alias('cpu_temp'),
            # F.lit(None).alias('gpu_temp'),
        ).withColumn("gpu_temp", F.lit(0))

    def write_to_postgres(self, batch_df, _):
        # Set config to write transformed incoming data
        batch_df.write \
            .format("jdbc") \
            .option("url", self.database_info['db_url']) \
            .option("dbtable", self.database_info['table']) \
            .option("user", self.database_info['user']) \
            .option("password", self.database_info['password']) \
            .option("driver", self.database_info['driver']) \
            .mode(self.database_info['mode']) \
            .save()

    def start_streaming(self):
        input_df = self.extract()
        processed_df = self.process(input_df)
        merged_df = self.transform(processed_df)

        # Write transformed dataframe (df_final) to Postgres
        streaming_query = merged_df.writeStream \
            .foreachBatch(self.write_to_postgres) \
            .outputMode("append") \
            .start()

        streaming_query.awaitTermination()


if __name__ == "__main__":
    config_filepath = 'config.json'

    config = load_config(config_file=config_filepath)
    if not config:
        raise ValueError(f'Config from {config_filepath} not fetched or parsed')

    process_data = Streaming_ETL(config=config)
    process_data.start_streaming()
