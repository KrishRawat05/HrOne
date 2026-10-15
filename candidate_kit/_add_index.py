from pymongo import MongoClient, ASCENDING
c = MongoClient('mongodb://localhost:27017')
db = c['attendance_test_100k']
db['employees'].create_index([('joined_on', ASCENDING)], name='joined_on_asc')
print('employees indexes:', list(db['employees'].index_information().keys()))
c.close()
